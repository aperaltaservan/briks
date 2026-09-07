"""Montajes: creación paso a paso consumiendo piezas del inventario.

Un montaje es una secuencia ordenada de pasos; cada paso declara qué piezas
usa. Mientras el montaje esté activo esas piezas quedan reservadas y no pueden
usarse en otro montaje. Pasar el montaje a "desmontado" las devuelve.
"""
from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from ..models import (
    BUILD_STATES,
    Build,
    BuildStep,
    BuildStepPart,
    Element,
    LegoSet,
    Placement,
    SetPart,
)
from . import catalog, inventory


class BuildError(Exception):
    """Error de negocio de montajes."""


class PartResolutionError(BuildError):
    """No se ha podido identificar una pieza sin ambigüedad."""

    def __init__(self, mensaje: str, candidatos: list[dict[str, Any]] | None = None) -> None:
        super().__init__(mensaje)
        self.candidatos = candidatos or []


# --------------------------------------------------------------------------
# Resolución de piezas indicadas en lenguaje natural
# --------------------------------------------------------------------------
def resolve_part_spec(session: Session, spec: dict[str, Any]) -> tuple[str, int]:
    """Convierte {descripcion|element_id, color, cantidad} en (element_id, cantidad).

    Si la descripción es ambigua, lanza PartResolutionError con los candidatos
    para que quien llama (normalmente el chat) pueda preguntar.
    """
    cantidad = int(spec.get("cantidad") or spec.get("quantity") or 1)
    if cantidad < 1:
        raise BuildError("La cantidad de una pieza debe ser al menos 1.")

    element_id = (spec.get("element_id") or "").strip() if spec.get("element_id") else None
    if element_id:
        if session.get(Element, element_id) is None:
            raise PartResolutionError(f"El elemento {element_id!r} no existe en el catálogo.")
        return element_id, cantidad

    descripcion = (spec.get("descripcion") or spec.get("pieza") or "").strip()
    color = (spec.get("color") or "").strip() or None
    if not descripcion and not color:
        raise PartResolutionError(
            "Cada pieza necesita un element_id o al menos una descripción."
        )

    candidatos = catalog.search_elements(
        session,
        query=descripcion,
        color=color,
        design_id=spec.get("design_id"),
        only_in_stock=True,
        limit=8,
    )
    if not candidatos:
        # Reintento sin exigir stock: quizá está en el catálogo pero no la tengo.
        sin_stock = catalog.search_elements(
            session, query=descripcion, color=color, design_id=spec.get("design_id"), limit=5
        )
        etiqueta = " ".join(x for x in (descripcion, color) if x)
        if sin_stock:
            raise PartResolutionError(
                f"'{etiqueta}' está en el catálogo pero no tienes unidades en inventario.",
                catalog.candidates_to_dicts(session, sin_stock),
            )
        raise PartResolutionError(f"No encuentro ninguna pieza que encaje con '{etiqueta}'.")

    mejor = candidatos[0]
    # Ambiguo si el segundo candidato puntúa casi igual que el primero.
    if len(candidatos) > 1 and candidatos[1].score >= mejor.score * 0.95:
        raise PartResolutionError(
            f"'{descripcion} {color or ''}' es ambiguo: hay varias piezas que encajan igual de bien. "
            "Indica el element_id concreto.",
            catalog.candidates_to_dicts(session, candidatos[:5]),
        )
    return mejor.element.element_id, cantidad


# --------------------------------------------------------------------------
# CRUD de montajes
# --------------------------------------------------------------------------
def create_build(session: Session, name: str, description: str | None = None) -> dict[str, Any]:
    nombre = (name or "").strip()
    if not nombre:
        raise BuildError("El montaje necesita un nombre.")
    build = Build(name=nombre, description=description)
    session.add(build)
    session.flush()
    return build_detail(session, build.id)


def set_build_status(session: Session, build_id: int, status: str) -> dict[str, Any]:
    build = _require_build(session, build_id)
    estado = (status or "").strip().lower()
    if estado not in BUILD_STATES:
        raise BuildError(f"Estado no válido: {status!r}. Usa uno de: {', '.join(BUILD_STATES)}.")
    build.status = estado
    session.flush()
    return build_detail(session, build.id)


def delete_build(session: Session, build_id: int) -> dict[str, Any]:
    build = _require_build(session, build_id)
    nombre = build.name
    session.delete(build)
    session.flush()
    return {"eliminado": True, "id": build_id, "nombre": nombre}


def _require_build(session: Session, build_id: int) -> Build:
    build = session.get(Build, int(build_id))
    if build is None:
        raise BuildError(f"No existe el montaje {build_id}.")
    return build


def _require_step(session: Session, step_id: int) -> BuildStep:
    step = session.get(BuildStep, int(step_id))
    if step is None:
        raise BuildError(f"No existe el paso {step_id}.")
    return step


# --------------------------------------------------------------------------
# Pasos
# --------------------------------------------------------------------------
def add_step(
    session: Session,
    build_id: int,
    title: str,
    parts: Iterable[dict[str, Any]] | None = None,
    instruction: str | None = None,
    position: int | None = None,
    permitir_faltantes: bool = False,
) -> dict[str, Any]:
    """Añade un paso al final (o en la posición indicada) validando disponibilidad."""
    build = _require_build(session, build_id)
    titulo = (title or "").strip()
    if not titulo:
        raise BuildError("El paso necesita un título.")

    especificaciones = list(parts or [])
    resueltas: list[tuple[str, int]] = []
    for spec in especificaciones:
        resueltas.append(resolve_part_spec(session, spec))

    # Agrupamos por elemento: el mismo paso puede repetir la misma pieza.
    necesario: dict[str, int] = {}
    for element_id, cantidad in resueltas:
        necesario[element_id] = necesario.get(element_id, 0) + cantidad

    faltantes = _faltantes(session, necesario)
    if faltantes and not permitir_faltantes:
        raise BuildError(
            "No hay piezas suficientes para este paso: "
            + "; ".join(
                f"{f['pieza']} ({f['color']}): necesitas {f['necesarias']}, disponibles {f['disponibles']}"
                for f in faltantes
            )
        )

    if position is None:
        maxima = session.scalar(
            select(func.max(BuildStep.position)).where(BuildStep.build_id == build.id)
        )
        posicion = (maxima or 0) + 1
    else:
        posicion = int(position)
        if posicion < 1:
            raise BuildError("La posición debe ser 1 o mayor.")
        _desplazar_desde(session, build.id, posicion)

    step = BuildStep(
        build_id=build.id, position=posicion, title=titulo, instruction=instruction
    )
    session.add(step)
    session.flush()

    for element_id, cantidad in necesario.items():
        session.add(BuildStepPart(step_id=step.id, element_id=element_id, quantity=cantidad))

    build.status = "en_progreso" if build.status == "planificado" else build.status
    session.flush()

    detalle = build_detail(session, build.id)
    detalle["paso_anadido"] = posicion
    if faltantes:
        detalle["aviso_faltantes"] = faltantes
    return detalle


def _desplazar_desde(session: Session, build_id: int, desde: int) -> None:
    """Hace hueco desplazando los pasos iguales o posteriores a `desde`.

    Se recorre en orden descendente para no chocar con la restricción única
    (build_id, position).
    """
    pasos = list(
        session.scalars(
            select(BuildStep)
            .where(BuildStep.build_id == build_id, BuildStep.position >= desde)
            .order_by(BuildStep.position.desc())
        )
    )
    for paso in pasos:
        paso.position += 1
        session.flush()


def update_step(
    session: Session,
    step_id: int,
    title: str | None = None,
    instruction: str | None = None,
    parts: Iterable[dict[str, Any]] | None = None,
    permitir_faltantes: bool = False,
) -> dict[str, Any]:
    """Corrige un paso ya creado. Sólo cambia lo que se indique.

    Al sustituir las piezas, primero se sueltan las que el paso tenía: así la
    disponibilidad se calcula sin que el paso compita consigo mismo. Si la
    validación falla, la transacción se deshace y el paso queda como estaba.
    """
    step = _require_step(session, step_id)

    if title is not None:
        titulo = title.strip()
        if not titulo:
            raise BuildError("El paso necesita un título.")
        step.title = titulo

    if instruction is not None:
        step.instruction = instruction.strip() or None

    if parts is not None:
        # Si el paso tiene piezas colocadas en el modelo 3D, sus cantidades las
        # gobierna el diseñador: cambiarlas aquí dejaría el modelo descuadrado.
        colocadas = session.scalar(
            select(func.count(Placement.id)).where(Placement.step_id == step.id)
        )
        if colocadas:
            raise BuildError(
                f"Este paso tiene {colocadas} piezas colocadas en el modelo 3D. "
                "Añádelas o quítalas desde el diseñador, no editando el paso."
            )
        for antigua in list(step.parts):
            session.delete(antigua)
        session.flush()

        necesario: dict[str, int] = {}
        for spec in parts:
            element_id, cantidad = resolve_part_spec(session, spec)
            necesario[element_id] = necesario.get(element_id, 0) + cantidad

        faltantes = _faltantes(session, necesario)
        if faltantes and not permitir_faltantes:
            raise BuildError(
                "No hay piezas suficientes para el paso: "
                + "; ".join(
                    f"{f['pieza']} ({f['color']}): necesitas {f['necesarias']}, "
                    f"disponibles {f['disponibles']}"
                    for f in faltantes
                )
            )
        for element_id, cantidad in necesario.items():
            session.add(BuildStepPart(step_id=step.id, element_id=element_id, quantity=cantidad))

    session.flush()
    return build_detail(session, step.build_id)


def remove_step(session: Session, step_id: int) -> dict[str, Any]:
    step = _require_step(session, step_id)
    build_id = step.build_id
    posicion = step.position
    session.delete(step)
    session.flush()
    # Recompactamos posiciones en orden ascendente.
    for paso in session.scalars(
        select(BuildStep)
        .where(BuildStep.build_id == build_id, BuildStep.position > posicion)
        .order_by(BuildStep.position.asc())
    ):
        paso.position -= 1
        session.flush()
    return build_detail(session, build_id)


def set_step_status(session: Session, step_id: int, status: str) -> dict[str, Any]:
    step = _require_step(session, step_id)
    estado = (status or "").strip().lower()
    if estado not in ("pendiente", "hecho"):
        raise BuildError("El estado de un paso debe ser 'pendiente' o 'hecho'.")
    step.status = estado
    session.flush()

    build = session.get(Build, step.build_id)
    pasos = list(session.scalars(select(BuildStep).where(BuildStep.build_id == build.id)))
    if pasos and all(p.status == "hecho" for p in pasos):
        build.status = "completado"
    elif build.status == "completado":
        build.status = "en_progreso"
    session.flush()
    return build_detail(session, build.id)


# --------------------------------------------------------------------------
# Consulta y viabilidad
# --------------------------------------------------------------------------
def _faltantes(session: Session, necesario: dict[str, int]) -> list[dict[str, Any]]:
    """Compara lo necesario con lo disponible y devuelve lo que no llega."""
    if not necesario:
        return []
    disponibilidad = inventory.availability(session, list(necesario))
    faltan: list[dict[str, Any]] = []
    for element_id, cantidad in necesario.items():
        disponible = disponibilidad[element_id]["disponible"]
        if disponible < cantidad:
            element = session.get(Element, element_id)
            faltan.append(
                {
                    "element_id": element_id,
                    "pieza": element.part.name if element else element_id,
                    "color": element.color.name if element else "?",
                    "necesarias": cantidad,
                    "disponibles": disponible,
                    "faltan": cantidad - disponible,
                }
            )
    return faltan


def build_detail(session: Session, build_id: int) -> dict[str, Any]:
    build = session.get(Build, int(build_id))
    if build is None:
        raise BuildError(f"No existe el montaje {build_id}.")

    pasos = list(
        session.scalars(
            select(BuildStep)
            .where(BuildStep.build_id == build.id)
            .order_by(BuildStep.position)
            .options(joinedload(BuildStep.parts).joinedload(BuildStepPart.element))
        ).unique()
    )
    colocaciones_por_paso = dict(
        session.execute(
            select(Placement.step_id, func.count(Placement.id))
            .where(Placement.build_id == build.id)
            .group_by(Placement.step_id)
        ).all()
    )

    total_piezas = 0
    pasos_json: list[dict[str, Any]] = []
    for paso in pasos:
        piezas = []
        for bp in paso.parts:
            element = bp.element
            total_piezas += bp.quantity
            piezas.append(
                {
                    "element_id": bp.element_id,
                    "pieza": element.part.name if element else bp.element_id,
                    "color": element.color.name if element else None,
                    "cantidad": bp.quantity,
                    "imagen": catalog.absolute_image_url(element.image_url) if element else None,
                }
            )
        pasos_json.append(
            {
                "id": paso.id,
                "posicion": paso.position,
                "titulo": paso.title,
                "instruccion": paso.instruction,
                "estado": paso.status,
                "piezas": piezas,
                "colocaciones": int(colocaciones_por_paso.get(paso.id, 0)),
            }
        )

    return {
        "id": build.id,
        "nombre": build.name,
        "descripcion": build.description,
        "estado": build.status,
        "placa": {"ancho": build.baseplate_w, "fondo": build.baseplate_d},
        "colocaciones": int(sum(colocaciones_por_paso.values())),
        "creado": build.created_at.isoformat(),
        "actualizado": build.updated_at.isoformat(),
        "num_pasos": len(pasos_json),
        "pasos_hechos": sum(1 for p in pasos_json if p["estado"] == "hecho"),
        "piezas_totales": total_piezas,
        "pasos": pasos_json,
    }


def list_builds(session: Session, status: str | None = None) -> list[dict[str, Any]]:
    stmt = select(Build).order_by(Build.updated_at.desc())
    if status:
        stmt = stmt.where(Build.status == status.strip().lower())
    resumen = []
    for build in session.scalars(stmt):
        pasos = session.scalar(
            select(func.count(BuildStep.id)).where(BuildStep.build_id == build.id)
        )
        piezas = (
            session.scalar(
                select(func.sum(BuildStepPart.quantity))
                .join(BuildStep, BuildStep.id == BuildStepPart.step_id)
                .where(BuildStep.build_id == build.id)
            )
            or 0
        )
        colocaciones = (
            session.scalar(select(func.count(Placement.id)).where(Placement.build_id == build.id))
            or 0
        )
        resumen.append(
            {
                "id": build.id,
                "nombre": build.name,
                "descripcion": build.description,
                "estado": build.status,
                "num_pasos": int(pasos or 0),
                "piezas_totales": int(piezas),
                "colocaciones": int(colocaciones),
                "actualizado": build.updated_at.isoformat(),
            }
        )
    return resumen


def check_build(session: Session, build_id: int) -> dict[str, Any]:
    """Comprueba si el montaje es viable con el inventario actual."""
    build = _require_build(session, build_id)
    necesario: dict[str, int] = {}
    filas = session.execute(
        select(BuildStepPart.element_id, func.sum(BuildStepPart.quantity))
        .join(BuildStep, BuildStep.id == BuildStepPart.step_id)
        .where(BuildStep.build_id == build.id)
        .group_by(BuildStepPart.element_id)
    ).all()
    for element_id, cantidad in filas:
        necesario[element_id] = int(cantidad or 0)

    # Excluimos el propio montaje: sus reservas no deben contar contra sí mismo.
    disponibilidad = inventory.availability(session, list(necesario), excluir_build=build.id)
    faltan = []
    for element_id, cantidad in necesario.items():
        libre = disponibilidad[element_id]["disponible"]
        if libre < cantidad:
            element = session.get(Element, element_id)
            faltan.append(
                {
                    "element_id": element_id,
                    "pieza": element.part.name if element else element_id,
                    "color": element.color.name if element else "?",
                    "necesarias": cantidad,
                    "disponibles": libre,
                    "faltan": cantidad - libre,
                }
            )

    return {
        "montaje": build.name,
        "id": build.id,
        "viable": not faltan,
        "referencias_usadas": len(necesario),
        "piezas_totales": sum(necesario.values()),
        "faltantes": faltan,
    }


# --------------------------------------------------------------------------
# Sugerencias a partir del inventario
# --------------------------------------------------------------------------
def buildable_sets(session: Session, umbral: float = 0.0) -> list[dict[str, Any]]:
    """¿Qué sets importados puedo montar con lo que tengo disponible ahora?"""
    resultados = []
    for lego_set in session.scalars(select(LegoSet)):
        lineas = list(session.scalars(select(SetPart).where(SetPart.set_number == lego_set.set_number)))
        if not lineas:
            continue
        necesario = {l.element_id: l.quantity for l in lineas}
        disponibilidad = inventory.availability(session, list(necesario))

        total = sum(necesario.values())
        cubiertas = 0
        faltan = []
        for element_id, cantidad in necesario.items():
            libre = disponibilidad[element_id]["disponible"]
            cubiertas += min(libre, cantidad)
            if libre < cantidad:
                element = session.get(Element, element_id)
                faltan.append(
                    {
                        "element_id": element_id,
                        "pieza": element.part.name if element else element_id,
                        "color": element.color.name if element else "?",
                        "faltan": cantidad - libre,
                    }
                )

        porcentaje = round(100 * cubiertas / total, 1) if total else 0.0
        if porcentaje >= umbral * 100:
            resultados.append(
                {
                    "set": lego_set.set_number,
                    "nombre": lego_set.name,
                    "piezas_totales": total,
                    "cobertura_pct": porcentaje,
                    "completo": not faltan,
                    "num_faltantes": len(faltan),
                    "faltantes": faltan[:15],
                }
            )

    resultados.sort(key=lambda r: -r["cobertura_pct"])
    return resultados
