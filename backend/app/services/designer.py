"""Diseñador 3D: piezas colocadas en un punto concreto del modelo.

Es la capa que convierte un montaje (una lista de pasos con cantidades) en un
modelo con volumen, al estilo de LEGO Digital Designer: cada pieza sabe dónde
está, qué ocupa y en qué paso se pone.

Reglas que se hacen cumplir aquí:
  - Todo cabe dentro de la placa base.
  - Dos piezas no pueden ocupar el mismo hueco.
  - Sólo se coloca lo que hay disponible en el inventario.
  - Las cantidades del paso (`BuildStepPart`, que es lo que reserva piezas) se
    mantienen en sintonía con las colocaciones: la reserva sube cuando se coloca
    más de lo que el paso declaraba y baja al retirar, pero dar sitio a una
    pieza que el paso ya pedía no la vuelve a apartar del inventario.

Coordenadas, en unidades LEGO y enteras:
  x, z -> studs desde la esquina de la placa.   y -> placas desde el suelo.

Aquí se coloca, se mueve una pieza y se lee el modelo. Rectificar en grupo lo
ya construido (mover una pared entera, repintarla, duplicarla) es `editor.py`,
y deshacer, `historial.py`: toda operación guarda antes una foto del modelo.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from ..models import (
    Build,
    BuildStep,
    BuildStepPart,
    Element,
    Placement,
)
from . import builds as builds_service
from . import catalog, geometry, historial, inventory, ldraw
from .builds import BuildError

# Nadie necesita una placa de 3 studs ni de 200: los límites evitan modelos
# absurdos y protegen al render del navegador.
PLACA_MIN = 8
PLACA_MAX = 96


class DesignError(BuildError):
    """Error de negocio del diseñador (choque, fuera de la placa, sin piezas)."""


# --------------------------------------------------------------------------
# Utilidades del modelo
#
# Lo de aquí no es privado a propósito: `editor.py` construye sus operaciones
# en grupo con estas mismas piezas (la forma de un molde, el volumen que ocupa
# lo colocado, la altura de apoyo, las reservas). Si cada módulo se hiciera las
# suyas, lo que se ve y lo que el motor considera ocupado dejarían de coincidir.
# --------------------------------------------------------------------------
def requiere_montaje(session: Session, build_id: int) -> Build:
    build = session.get(Build, int(build_id))
    if build is None:
        raise DesignError(f"No existe el montaje {build_id}.")
    return build


def forma_de(nombre_pieza: str, design_id: str) -> geometry.Forma:
    """Volumen que ocupa una pieza.

    El ancho y el fondo salen del nombre del molde, porque describen el
    rectángulo de tetones sobre el que la pieza se ancla, que es lo que importa
    para la rejilla.

    La altura, en cambio, sale de la geometría real siempre que se conozca: el
    nombre miente a menudo. "CORNER PLATE 1X2X2" no es un bloque de dos
    ladrillos, es una placa de una sola placa de alto, y creerse el nombre deja
    flotando en el aire todo lo que se apoye encima.
    """
    forma = geometry.forma_de_nombre(nombre_pieza)
    reales = ldraw.medidas(design_id)
    if not reales:
        return forma
    alto = ldraw.placas_de_alto(reales["alto"])
    if alto == forma.alto:
        return forma
    return replace(forma, alto=alto, exacta=forma.exacta and forma.familia != geometry.FAMILIA_OTRA)


def forma_de_elemento(session: Session, element_id: str) -> geometry.Forma:
    """Volumen de un elemento del catálogo."""
    element = session.get(Element, str(element_id).strip())
    if element is None:
        raise DesignError(f"El elemento {element_id!r} no está en el catálogo.")
    return forma_de(element.part.name, element.design_id)


def paso_destino(
    session: Session, build: Build, paso_id: int | None, titulo_paso: str | None
) -> BuildStep:
    """Paso en el que caen las piezas: el indicado, uno nuevo, o el último.

    Si el montaje no tiene ninguno se crea el primero, porque una colocación
    siempre pertenece a un paso: es lo que permite reconstruir las
    instrucciones después.
    """
    if paso_id:
        paso = session.get(BuildStep, int(paso_id))
        if paso is None or paso.build_id != build.id:
            raise DesignError(f"El paso {paso_id} no pertenece a este montaje.")
        if titulo_paso:
            paso.title = titulo_paso.strip() or paso.title
        return paso

    if titulo_paso:
        maxima = session.scalar(
            select(func.max(BuildStep.position)).where(BuildStep.build_id == build.id)
        )
        paso = BuildStep(build_id=build.id, position=(maxima or 0) + 1, title=titulo_paso.strip())
        session.add(paso)
        session.flush()
        return paso

    ultimo = session.scalar(
        select(BuildStep)
        .where(BuildStep.build_id == build.id)
        .order_by(BuildStep.position.desc())
        .limit(1)
    )
    if ultimo is not None:
        return ultimo

    paso = BuildStep(build_id=build.id, position=1, title="Paso 1")
    session.add(paso)
    session.flush()
    return paso


def colocaciones_de(session: Session, build_id: int) -> list[Placement]:
    return list(
        session.scalars(
            select(Placement)
            .where(Placement.build_id == build_id)
            .options(joinedload(Placement.element).joinedload(Element.part))
            .order_by(Placement.id)
        ).unique()
    )


# Relieve de cada molde en su propia rejilla, ya casado con el footprint del
# nombre. Se calcula una vez por molde y no cambia.
_PERFILES: dict[tuple[str, str], dict[tuple[int, int], tuple[int, int]]] = {}


def ajuste_de_dibujo(forma: geometry.Forma, reales: dict[str, float]) -> dict[str, float]:
    """Cómo hay que girar y correr la malla para que case con su casilla.

    LDraw y el nombre del catálogo no siempre coinciden en qué lado es el ancho,
    y algunos moldes tienen su origen en un vértice en vez de en el centro. Esto
    se calcula aquí, una sola vez, y lo usan por igual el motor de colocación y
    el dibujo del navegador: si cada uno lo hiciera por su cuenta, lo que se ve
    y lo que choca acabarían siendo cosas distintas.
    """
    directo = abs(reales["ancho"] - forma.ancho) + abs(reales["fondo"] - forma.fondo)
    girado = abs(reales["ancho"] - forma.fondo) + abs(reales["fondo"] - forma.ancho)
    giro = 90 if girado + 0.05 < directo else 0

    x0, x1 = reales["min_x"], reales["min_x"] + reales["ancho"]
    z0, z1 = reales["min_z"], reales["min_z"] + reales["fondo"]
    if giro:
        # Al girar 90 grados, (x, z) pasa a (z, -x).
        x0, x1, z0, z1 = z0, z1, -x1, -x0

    # Se centra la pieza en su casilla, salvo que sobresalga de ella: entonces
    # el origen de LDraw es el bueno (una barra, un eje que asoma).
    dx = -(x0 + x1) / 2 if x1 - x0 <= forma.ancho + 0.1 else 0.0
    dz = -(z0 + z1) / 2 if z1 - z0 <= forma.fondo + 0.1 else 0.0
    return {"giro": giro, "dx": round(dx, 3), "dz": round(dz, 3)}


def _medidas_con_origen(design_id: str) -> dict[str, float] | None:
    """Medidas del molde más la esquina de su caja, que hace falta para alinear."""
    reales = ldraw.medidas(design_id)
    relieve = ldraw.perfil(design_id)
    if not reales or not relieve:
        return None
    return {**reales, "min_x": relieve["origen"][0], "min_z": relieve["origen"][1]}


def perfil_de(nombre_pieza: str, design_id: str) -> dict[tuple[int, int], tuple[int, int]]:
    """Relieve de la pieza por casillas: {(i, j): (desde, hasta)} en placas.

    Las casillas van de (0,0) a (ancho-1, fondo-1) del footprint, sin girar. Una
    pieza sin geometría conocida se trata como el bloque macizo de su caja, que
    es lo más prudente.
    """
    clave = (nombre_pieza, design_id)
    if clave in _PERFILES:
        return _PERFILES[clave]

    forma = forma_de(nombre_pieza, design_id)
    macizo = {(i, j): (0, forma.alto) for i in range(forma.ancho) for j in range(forma.fondo)}

    crudo = ldraw.perfil(design_id)
    reales = _medidas_con_origen(design_id)
    if not crudo or not reales:
        _PERFILES[clave] = macizo
        return macizo

    ajuste = ajuste_de_dibujo(forma, reales)
    x0, z0 = crudo["origen"]
    columnas: dict[tuple[int, int], tuple[int, int]] = {}

    for j in range(crudo["fondo"]):
        for i in range(crudo["ancho"]):
            arriba = crudo["arriba"][j][i]
            if arriba is None:
                continue
            abajo = crudo["abajo"][j][i] or 0.0
            cx, cz = x0 + i + 0.5, z0 + j + 0.5
            if ajuste["giro"]:
                cx, cz = cz, -cx
            destino_i = int(cx + ajuste["dx"] + forma.ancho / 2)
            destino_j = int(cz + ajuste["dz"] + forma.fondo / 2)
            if not (0 <= destino_i < forma.ancho and 0 <= destino_j < forma.fondo):
                continue
            # La base no es plana: una placa curva 2x2x2/3 tiene la mitad de
            # atrás una placa más alta que la de delante, y debajo cabe una
            # placa. Medio stud de hueco ya cuenta como una placa: los labios
            # finos que dejan las paredes no son material que estorbe.
            desde = int(abajo / ldraw.ALTO_PLACA + 0.5 + 1e-6)
            hasta = ldraw.placas_de_alto(arriba)
            actual = columnas.get((destino_i, destino_j))
            columnas[(destino_i, destino_j)] = (
                (desde, hasta)
                if actual is None
                else (min(actual[0], desde), max(actual[1], hasta))
            )

    resultado = columnas or macizo
    _PERFILES[clave] = resultado
    return resultado


def _girar_celda(i: int, j: int, ancho: int, fondo: int, rotacion: int) -> tuple[int, int]:
    """La misma casilla después de girar la pieza sobre su eje vertical."""
    if rotacion == 90:
        return fondo - 1 - j, i
    if rotacion == 180:
        return ancho - 1 - i, fondo - 1 - j
    if rotacion == 270:
        return j, ancho - 1 - i
    return i, j


class Volumen:
    """El sitio que ocupa una pieza: por cada casilla, desde y hasta qué altura."""

    __slots__ = ("columnas", "etiqueta", "colocacion_id")

    def __init__(
        self,
        columnas: dict[tuple[int, int], tuple[int, int]],
        etiqueta: str,
        colocacion_id: int | None = None,
    ) -> None:
        self.columnas = columnas
        self.etiqueta = etiqueta
        self.colocacion_id = colocacion_id

    def choca_con(self, otras: dict[tuple[int, int], tuple[int, int]]) -> bool:
        """¿Comparten alguna casilla en la que además se crucen en altura?"""
        for celda, (desde, hasta) in otras.items():
            tramo = self.columnas.get(celda)
            if tramo and not (hasta <= tramo[0] or tramo[1] <= desde):
                return True
        return False


def columnas_de(
    nombre_pieza: str, design_id: str, x: int, y: int, z: int, rotacion: int
) -> dict[tuple[int, int], tuple[int, int]]:
    """Casillas del tablero que ocupa una pieza puesta ahí, con su tramo de altura."""
    forma = forma_de(nombre_pieza, design_id)
    relieve = perfil_de(nombre_pieza, design_id)
    salida: dict[tuple[int, int], tuple[int, int]] = {}
    for (i, j), (desde, hasta) in relieve.items():
        gi, gj = _girar_celda(i, j, forma.ancho, forma.fondo, rotacion)
        salida[(x + gi, z + gj)] = (y + desde, y + hasta)
    return salida


def ocupacion(
    session: Session, colocaciones: Iterable[Placement], ignorar: set[int] | None = None
) -> list[Volumen]:
    """Volumen ocupado por cada pieza ya colocada."""
    ignorar = ignorar or set()
    salida: list[Volumen] = []
    for p in colocaciones:
        if p.id in ignorar:
            continue
        salida.append(
            Volumen(
                columnas_de(p.element.part.name, p.element.design_id, p.x, p.y, p.z, p.rotation),
                f"{p.element.part.name} (colocación {p.id})",
                p.id,
            )
        )
    return salida


def altura_de_apoyo(
    ocupado: list[Volumen], columnas: dict[tuple[int, int], tuple[int, int]]
) -> int:
    """Dónde cae una pieza soltada sobre el modelo.

    Se mira casilla a casilla: lo que importa es lo que hay justo debajo de cada
    parte de la pieza, no lo más alto de la pieza vecina.
    """
    # La pieza baja hasta que su parte más baja toca algo. Una casilla cuya
    # base está una placa más alta (la mitad trasera de una placa curva) puede
    # pasar por encima de una placa sin que eso la levante.
    altura = 0
    for celda, (desde, _) in columnas.items():
        tope = 0
        for volumen in ocupado:
            tramo = volumen.columnas.get(celda)
            if tramo:
                tope = max(tope, tramo[1])
        altura = max(altura, tope - desde)
    return altura


def casillas_en_voladizo(
    ocupado: list[Volumen], columnas: dict[tuple[int, int], tuple[int, int]]
) -> list[tuple[int, int]]:
    """Casillas de la pieza que no tienen nada justo debajo.

    Una pieza puede quedar a caballo entre el borde de otra y el vacío. Se
    sostiene —en LEGO también—, pero conviene decirlo: casi siempre es que se ha
    colocado una casilla más allá de donde se quería.
    """
    huecas = []
    for celda, (desde, _) in columnas.items():
        if desde == 0:
            continue  # apoyada directamente en la placa base
        if not any(v.columnas.get(celda, (0, 0))[1] >= desde for v in ocupado):
            huecas.append(celda)
    return huecas


def colocadas_en_paso(session: Session, step_id: int, element_id: str) -> int:
    return int(
        session.scalar(
            select(func.count(Placement.id)).where(
                Placement.step_id == step_id, Placement.element_id == element_id
            )
        )
        or 0
    )


def reserva_tras_colocar(session: Session, step_id: int, element_id: str) -> None:
    """Sube la reserva del paso sólo si las colocaciones superan lo declarado.

    Un paso escrito antes ("2 placas 2x6") ya tiene esas piezas reservadas: al
    darles sitio en el modelo no se vuelven a apartar del inventario. La reserva
    sólo crece cuando se coloca más de lo que el paso decía.
    """
    colocadas = colocadas_en_paso(session, step_id, element_id)
    linea = session.scalar(
        select(BuildStepPart).where(
            BuildStepPart.step_id == step_id, BuildStepPart.element_id == element_id
        )
    )
    if linea is None:
        session.add(
            BuildStepPart(step_id=step_id, element_id=element_id, quantity=max(1, colocadas))
        )
    elif linea.quantity < colocadas:
        linea.quantity = colocadas
    session.flush()


def reserva_tras_quitar(session: Session, step_id: int, element_id: str) -> None:
    """Baja la reserva una unidad, sin caer por debajo de lo que sigue colocado."""
    colocadas = colocadas_en_paso(session, step_id, element_id)
    linea = session.scalar(
        select(BuildStepPart).where(
            BuildStepPart.step_id == step_id, BuildStepPart.element_id == element_id
        )
    )
    if linea is None:
        return
    nueva = max(colocadas, linea.quantity - 1)
    if nueva <= 0:
        session.delete(linea)
    else:
        linea.quantity = nueva
    session.flush()


def incremento_de_reserva(
    session: Session, step_id: int, extra: dict[str, int]
) -> dict[str, int]:
    """Cuánto hay que apartar del inventario para colocar `extra` en ese paso.

    Del inventario sólo se aparta lo que exceda de lo que el paso ya tenía
    reservado: dar sitio a una pieza que el paso declaraba no la vuelve a
    reservar.
    """
    declarado = {
        linea.element_id: linea.quantity
        for linea in session.scalars(select(BuildStepPart).where(BuildStepPart.step_id == step_id))
    }
    incremento: dict[str, int] = {}
    for element_id, unidades in extra.items():
        pendiente = (
            colocadas_en_paso(session, step_id, element_id)
            + unidades
            - declarado.get(element_id, 0)
        )
        if pendiente > 0:
            incremento[element_id] = pendiente
    return incremento


def _reservado_por_el_montaje(session: Session, build_id: int) -> dict[str, int]:
    filas = session.execute(
        select(BuildStepPart.element_id, func.sum(BuildStepPart.quantity))
        .join(BuildStep, BuildStep.id == BuildStepPart.step_id)
        .where(BuildStep.build_id == build_id)
        .group_by(BuildStepPart.element_id)
    ).all()
    return {element_id: int(total or 0) for element_id, total in filas}


def comprobar_disponibilidad(
    session: Session, build: Build, extra: dict[str, int]
) -> list[dict[str, Any]]:
    """¿Hay piezas para lo que se quiere añadir?

    Se compara contra el inventario libre SIN contar este montaje, y se le suma
    lo que el propio montaje ya tenía reservado. Así el cálculo vale igual si el
    montaje está activo que si estaba desmontado.
    """
    if not extra:
        return []
    ya = _reservado_por_el_montaje(session, build.id)
    libre = inventory.availability(session, list(extra), excluir_build=build.id)

    faltan: list[dict[str, Any]] = []
    for element_id, cantidad in extra.items():
        necesarias = ya.get(element_id, 0) + cantidad
        disponibles = libre[element_id]["disponible"]
        if necesarias > disponibles:
            element = session.get(Element, element_id)
            faltan.append(
                {
                    "element_id": element_id,
                    "pieza": element.part.name if element else element_id,
                    "color": element.color.name if element else "?",
                    "necesarias": necesarias,
                    "disponibles": disponibles,
                    "faltan": necesarias - disponibles,
                }
            )
    return faltan


# --------------------------------------------------------------------------
# Colocar, mover y quitar
# --------------------------------------------------------------------------
def colocar_piezas(
    session: Session,
    build_id: int,
    piezas: list[dict[str, Any]],
    paso_id: int | None = None,
    titulo_paso: str | None = None,
    permitir_faltantes: bool = False,
) -> dict[str, Any]:
    """Coloca una o varias piezas en el modelo, todas en el mismo paso.

    Cada entrada es {'element_id' | 'descripcion' (+ 'color'), 'x', 'z', 'y'?,
    'rotacion'?}. Sin 'y' la pieza se apoya sobre lo que haya debajo, que es
    como se construye de verdad.

    O entra el lote entero o no entra ninguna: una colocación a medias dejaría
    el modelo incoherente con las reservas.
    """
    build = requiere_montaje(session, build_id)
    if not piezas:
        raise DesignError("No has indicado ninguna pieza que colocar.")

    paso = paso_destino(session, build, paso_id, titulo_paso)
    ocupado = ocupacion(session, colocaciones_de(session, build.id))
    extra: dict[str, int] = {}
    nuevas: list[dict[str, Any]] = []
    avisos: list[str] = []

    for indice, spec in enumerate(piezas, start=1):
        element_id, _ = builds_service.resolve_part_spec(session, {**spec, "cantidad": 1})
        forma = forma_de_elemento(session, element_id)
        rotacion = geometry.normalizar_rotacion(spec.get("rotacion") or spec.get("rotation"))

        try:
            x = int(spec["x"])
            z = int(spec["z"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DesignError(f"La pieza {indice} necesita coordenadas enteras 'x' y 'z'.") from exc

        ancho, fondo = forma.rotada(rotacion)
        if x < 0 or z < 0 or x + ancho > build.baseplate_w or z + fondo > build.baseplate_d:
            raise DesignError(
                f"La pieza {indice} ({ancho}x{fondo} en {x},{z}) se sale de la placa "
                f"de {build.baseplate_w}x{build.baseplate_d}."
            )

        element = session.get(Element, element_id)
        apoyo = altura_de_apoyo(
            ocupado, columnas_de(element.part.name, element.design_id, x, 0, z, rotacion)
        )
        y_pedida = spec.get("y")
        y = apoyo if y_pedida is None else int(y_pedida)
        if y < 0:
            raise DesignError(f"La pieza {indice} no puede estar por debajo de la placa.")

        columnas = columnas_de(element.part.name, element.design_id, x, y, z, rotacion)
        for volumen in ocupado:
            if volumen.choca_con(columnas):
                raise DesignError(
                    f"La pieza {indice} choca en ({x},{y},{z}) con {volumen.etiqueta}."
                )

        huecas = casillas_en_voladizo(ocupado, columnas)
        if huecas and len(huecas) == len(columnas):
            avisos.append(f"La pieza {indice} queda al aire en ({x},{y},{z}): no hay nada debajo.")
        elif huecas:
            avisos.append(
                f"La pieza {indice} queda en voladizo: {len(huecas)} de sus "
                f"{len(columnas)} casillas no tienen nada debajo."
            )

        # Sobre qué descansa: es lo que explica por qué ha subido, si ha subido.
        # Descansa sobre lo que toque la base de cada casilla, que no siempre
        # está a la misma altura que la pieza.
        apoyada_sobre = sorted(
            {
                v.etiqueta
                for v in ocupado
                if any(
                    v.columnas.get(celda, (0, -1))[1] == desde
                    for celda, (desde, _) in columnas.items()
                )
            }
        )

        extra[element_id] = extra.get(element_id, 0) + 1
        nuevas.append(
            {
                "element_id": element_id,
                "x": x,
                "y": y,
                "z": z,
                "rotacion": rotacion,
                "apoyada_sobre": apoyada_sobre,
                "casillas_al_aire": len(huecas),
            }
        )
        # Cuenta para las siguientes del lote: dos piezas del mismo lote
        # tampoco pueden pisarse.
        ocupado.append(Volumen(columnas, f"la pieza {indice} de este mismo lote"))

    faltantes = comprobar_disponibilidad(
        session, build, incremento_de_reserva(session, paso.id, extra)
    )
    if faltantes and not permitir_faltantes:
        raise DesignError(
            "No hay piezas suficientes: "
            + "; ".join(
                f"{f['pieza']} ({f['color']}): necesitas {f['necesarias']}, "
                f"disponibles {f['disponibles']}"
                for f in faltantes
            )
        )

    historial.registrar(
        session, build.id, f"Colocar {len(nuevas)} pieza{'' if len(nuevas) == 1 else 's'}"
    )

    colocadas = []
    for nueva in nuevas:
        placement = Placement(
            build_id=build.id,
            step_id=paso.id,
            element_id=nueva["element_id"],
            x=nueva["x"],
            y=nueva["y"],
            z=nueva["z"],
            rotation=nueva["rotacion"],
        )
        session.add(placement)
        session.flush()
        reserva_tras_colocar(session, paso.id, nueva["element_id"])
        colocadas.append(placement.id)

    if build.status in ("planificado", "desmontado"):
        build.status = "en_progreso"
    session.flush()

    resultado: dict[str, Any] = {
        "montaje_id": build.id,
        "paso_id": paso.id,
        "paso": paso.title,
        "colocadas": len(colocadas),
        "ids": colocadas,
        # Dónde ha quedado cada una y por qué: si ha subido es porque descansa
        # sobre algo, y aquí se dice sobre qué.
        "detalle": [
            {
                "id": cid,
                "x": nueva["x"],
                "y": nueva["y"],
                "z": nueva["z"],
                "apoyada_sobre": nueva["apoyada_sobre"],
                "casillas_al_aire": nueva["casillas_al_aire"],
            }
            for cid, nueva in zip(colocadas, nuevas)
        ],
    }
    if avisos:
        resultado["avisos"] = avisos
    if faltantes:
        resultado["aviso_faltantes"] = faltantes
    return resultado


def mover_pieza(
    session: Session,
    colocacion_id: int,
    x: int | None = None,
    z: int | None = None,
    y: int | None = None,
    rotacion: int | None = None,
) -> dict[str, Any]:
    """Cambia de sitio una pieza ya colocada, validando de nuevo el hueco."""
    placement = session.get(Placement, int(colocacion_id))
    if placement is None:
        raise DesignError(f"No existe la colocación {colocacion_id}.")
    build = requiere_montaje(session, placement.build_id)

    forma = forma_de_elemento(session, placement.element_id)
    nueva_rot = (
        geometry.normalizar_rotacion(rotacion) if rotacion is not None else placement.rotation
    )
    nuevo_x = placement.x if x is None else int(x)
    nuevo_z = placement.z if z is None else int(z)

    ancho, fondo = forma.rotada(nueva_rot)
    if (
        nuevo_x < 0
        or nuevo_z < 0
        or nuevo_x + ancho > build.baseplate_w
        or nuevo_z + fondo > build.baseplate_d
    ):
        raise DesignError(
            f"La pieza se saldría de la placa de {build.baseplate_w}x{build.baseplate_d}."
        )

    # La pieza no debe estorbarse a sí misma al recalcular apoyo y choques.
    ocupado = ocupacion(session, colocaciones_de(session, build.id), ignorar={placement.id})
    nombre, design_id = placement.element.part.name, placement.element.design_id
    if y is None:
        nuevo_y = altura_de_apoyo(
            ocupado, columnas_de(nombre, design_id, nuevo_x, 0, nuevo_z, nueva_rot)
        )
    else:
        nuevo_y = int(y)
    if nuevo_y < 0:
        raise DesignError("La pieza no puede estar por debajo de la placa.")

    columnas = columnas_de(nombre, design_id, nuevo_x, nuevo_y, nuevo_z, nueva_rot)
    for volumen in ocupado:
        if volumen.choca_con(columnas):
            raise DesignError(f"Ahí choca con {volumen.etiqueta}.")

    historial.registrar(session, build.id, f"Mover {placement.element.part.name}")
    placement.x, placement.y, placement.z, placement.rotation = (
        nuevo_x,
        nuevo_y,
        nuevo_z,
        nueva_rot,
    )
    session.flush()
    return {
        "id": placement.id,
        "montaje_id": build.id,
        "x": placement.x,
        "y": placement.y,
        "z": placement.z,
        "rotacion": placement.rotation,
    }


def quitar_pieza(session: Session, colocacion_id: int) -> dict[str, Any]:
    """Retira una pieza del modelo y devuelve su unidad al inventario disponible."""
    placement = session.get(Placement, int(colocacion_id))
    if placement is None:
        raise DesignError(f"No existe la colocación {colocacion_id}.")
    datos = {
        "id": placement.id,
        "montaje_id": placement.build_id,
        "element_id": placement.element_id,
        "paso_id": placement.step_id,
    }
    historial.registrar(session, placement.build_id, f"Quitar {placement.element.part.name}")
    session.delete(placement)
    session.flush()
    reserva_tras_quitar(session, datos["paso_id"], datos["element_id"])
    return {"eliminada": True, **datos}


def vaciar(session: Session, build_id: int, paso_id: int | None = None) -> dict[str, Any]:
    """Quita todas las piezas del modelo, o sólo las de un paso."""
    build = requiere_montaje(session, build_id)
    stmt = select(Placement).where(Placement.build_id == build.id)
    if paso_id:
        stmt = stmt.where(Placement.step_id == int(paso_id))
    a_borrar = list(session.scalars(stmt))
    if a_borrar:
        historial.registrar(
            session,
            build.id,
            f"Vaciar {'el paso' if paso_id else 'el modelo'} ({len(a_borrar)} piezas)",
        )

    borradas = 0
    afectados: set[tuple[int, str]] = set()
    for placement in a_borrar:
        afectados.add((placement.step_id, placement.element_id))
        session.delete(placement)
        borradas += 1
    session.flush()
    for step_id, element_id in afectados:
        # Ya no queda ninguna colocación de esa pieza en el paso: la reserva
        # baja hasta lo que siga declarado a mano, o desaparece.
        reserva_tras_quitar(session, step_id, element_id)
    return {"montaje_id": build.id, "paso_id": paso_id, "eliminadas": borradas}


def configurar_placa(session: Session, build_id: int, ancho: int, fondo: int) -> dict[str, Any]:
    """Cambia el tamaño de la placa base, si no deja piezas fuera."""
    build = requiere_montaje(session, build_id)
    ancho, fondo = int(ancho), int(fondo)
    if not (PLACA_MIN <= ancho <= PLACA_MAX and PLACA_MIN <= fondo <= PLACA_MAX):
        raise DesignError(f"La placa debe medir entre {PLACA_MIN} y {PLACA_MAX} studs por lado.")

    for placement in colocaciones_de(session, build.id):
        forma = forma_de(placement.element.part.name, placement.element.design_id)
        p_ancho, p_fondo = forma.rotada(placement.rotation)
        if placement.x + p_ancho > ancho or placement.z + p_fondo > fondo:
            raise DesignError(
                f"No se puede encoger la placa: {placement.element.part.name} "
                f"(colocación {placement.id}) quedaría fuera."
            )

    historial.registrar(session, build.id, f"Placa a {ancho}x{fondo}")
    build.baseplate_w, build.baseplate_d = ancho, fondo
    session.flush()
    return {"montaje_id": build.id, "placa": {"ancho": ancho, "fondo": fondo}}


# --------------------------------------------------------------------------
# Lectura del modelo
# --------------------------------------------------------------------------
def _ajuste_de(element: Element, forma: geometry.Forma) -> dict[str, float] | None:
    reales = _medidas_con_origen(element.design_id)
    return ajuste_de_dibujo(forma, reales) if reales else None


def pieza_a_dict(placement: Placement, forma: geometry.Forma) -> dict[str, Any]:
    element = placement.element
    columnas = columnas_de(
        element.part.name,
        element.design_id,
        placement.x,
        placement.y,
        placement.z,
        placement.rotation,
    )
    return {
        "id": placement.id,
        "paso_id": placement.step_id,
        "element_id": placement.element_id,
        "design_id": element.design_id,
        "pieza": element.part.name,
        "color": element.color.name,
        "color_hex": element.color.hex_code,
        "transparente": bool(element.color.is_transparent),
        "imagen": catalog.absolute_image_url(element.image_url),
        "x": placement.x,
        "y": placement.y,
        "z": placement.z,
        "rotacion": placement.rotation,
        "forma": forma.to_dict(),
        # Cómo se alinea la malla con la casilla y qué relieve ocupa: el
        # navegador dibuja y previsualiza con lo mismo que valida el motor.
        "ajuste": _ajuste_de(element, forma),
        "columnas": [[x, z, desde, hasta] for (x, z), (desde, hasta) in columnas.items()],
    }


def _huella(pieza: dict[str, Any]) -> tuple[int, int]:
    """Ancho y fondo ya girados de una pieza serializada."""
    forma = pieza["forma"]
    if pieza["rotacion"] % 180 == 90:
        return forma["fondo"], forma["ancho"]
    return forma["ancho"], forma["fondo"]


def modelo(session: Session, build_id: int, con_mapa: bool = False) -> dict[str, Any]:
    """El modelo completo: placa, piezas colocadas y pasos, listo para dibujar."""
    build = requiere_montaje(session, build_id)
    colocaciones = colocaciones_de(session, build.id)

    piezas = []
    alto_max = 0
    for placement in colocaciones:
        forma = forma_de(placement.element.part.name, placement.element.design_id)
        alto_max = max(alto_max, placement.y + forma.alto)
        piezas.append(pieza_a_dict(placement, forma))

    pasos = [
        {
            "id": paso.id,
            "posicion": paso.position,
            "titulo": paso.title,
            "instruccion": paso.instruction,
            "estado": paso.status,
            "colocaciones": sum(1 for p in colocaciones if p.step_id == paso.id),
        }
        for paso in session.scalars(
            select(BuildStep).where(BuildStep.build_id == build.id).order_by(BuildStep.position)
        )
    ]

    datos: dict[str, Any] = {
        "montaje_id": build.id,
        "nombre": build.name,
        "estado": build.status,
        "placa": {"ancho": build.baseplate_w, "fondo": build.baseplate_d},
        "altura_maxima": alto_max,
        "total_piezas": len(piezas),
        "pasos": pasos,
        "piezas": piezas,
        # Qué se puede deshacer ahora mismo: el editor lo mira para saber si
        # tiene sentido ofrecer los botones de deshacer y rehacer.
        "historial": historial.estado(session, build.id),
    }
    if con_mapa:
        datos["mapa"] = mapa_por_capas(piezas)
    return datos


def mapa_por_capas(piezas: list[dict[str, Any]], lado_max: int = 40) -> list[dict[str, Any]]:
    """Planta en texto de cada capa, para poder «ver» el modelo desde el chat.

    Cada pieza se dibuja con una letra y se acompaña de su leyenda. Sin esto,
    quien construye desde una conversación no tiene forma de comprobar lo que
    lleva hecho.
    """
    if not piezas:
        return []

    letras = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    alto_total = max(p["y"] + p["forma"]["alto"] for p in piezas)
    x0 = min(p["x"] for p in piezas)
    z0 = min(p["z"] for p in piezas)
    x1 = max(p["x"] + _huella(p)[0] for p in piezas)
    z1 = max(p["z"] + _huella(p)[1] for p in piezas)
    ancho = min(x1 - x0, lado_max)
    fondo = min(z1 - z0, lado_max)

    capas = []
    for nivel in range(alto_total):
        presentes = [p for p in piezas if p["y"] <= nivel < p["y"] + p["forma"]["alto"]]
        if not presentes:
            continue
        rejilla = [["." for _ in range(ancho)] for _ in range(fondo)]
        leyenda: dict[str, str] = {}
        for indice, pieza in enumerate(presentes):
            marca = letras[indice % len(letras)]
            leyenda[marca] = f"{pieza['pieza']} · {pieza['color']} (#{pieza['id']})"
            p_ancho, p_fondo = _huella(pieza)
            for dx in range(p_ancho):
                for dz in range(p_fondo):
                    cx, cz = pieza["x"] + dx - x0, pieza["z"] + dz - z0
                    if 0 <= cx < ancho and 0 <= cz < fondo:
                        rejilla[cz][cx] = marca
        capas.append(
            {
                "nivel": nivel,
                "origen": {"x": x0, "z": z0},
                "filas": ["".join(fila) for fila in rejilla],
                "leyenda": leyenda,
            }
        )
    return capas


def instrucciones(session: Session, build_id: int) -> dict[str, Any]:
    """El montaje como un manual: qué piezas entran en cada paso y dónde van.

    Cada paso lleva las piezas nuevas (con su imagen y cantidad, como el
    recuadro de piezas de los manuales) y cuántas había ya montadas, para poder
    dibujar lo anterior en gris y lo nuevo en color.
    """
    build = requiere_montaje(session, build_id)
    colocaciones = colocaciones_de(session, build.id)
    formas = {
        p.id: forma_de(p.element.part.name, p.element.design_id)
        for p in colocaciones
    }

    pasos_db = list(
        session.scalars(
            select(BuildStep).where(BuildStep.build_id == build.id).order_by(BuildStep.position)
        )
    )

    acumulado = 0
    pasos: list[dict[str, Any]] = []
    for paso in pasos_db:
        nuevas = [p for p in colocaciones if p.step_id == paso.id]

        # Recuadro de piezas: agrupadas por elemento, como en un manual real.
        resumen: dict[str, dict[str, Any]] = {}
        for placement in nuevas:
            entrada = resumen.setdefault(
                placement.element_id,
                {
                    "element_id": placement.element_id,
                    "pieza": placement.element.part.name,
                    "color": placement.element.color.name,
                    "color_hex": placement.element.color.hex_code,
                    "imagen": catalog.absolute_image_url(placement.element.image_url),
                    "cantidad": 0,
                },
            )
            entrada["cantidad"] += 1

        # Piezas que el paso declara pero que aún no están puestas en el 3D.
        colocadas_por_elemento = {k: v["cantidad"] for k, v in resumen.items()}
        sin_colocar = []
        for linea in session.scalars(
            select(BuildStepPart).where(BuildStepPart.step_id == paso.id)
        ):
            pendientes = linea.quantity - colocadas_por_elemento.get(linea.element_id, 0)
            if pendientes > 0:
                element = session.get(Element, linea.element_id)
                sin_colocar.append(
                    {
                        "element_id": linea.element_id,
                        "pieza": element.part.name if element else linea.element_id,
                        "color": element.color.name if element else None,
                        "color_hex": element.color.hex_code if element else None,
                        "imagen": catalog.absolute_image_url(element.image_url) if element else None,
                        "cantidad": pendientes,
                    }
                )

        pasos.append(
            {
                "id": paso.id,
                "posicion": paso.position,
                "titulo": paso.title,
                "instruccion": paso.instruction,
                "estado": paso.status,
                "piezas_del_paso": sorted(resumen.values(), key=lambda p: -p["cantidad"]),
                "sin_colocar": sin_colocar,
                "nuevas": [pieza_a_dict(p, formas[p.id]) for p in nuevas],
                "piezas_previas": acumulado,
            }
        )
        acumulado += len(nuevas)

    return {
        "montaje_id": build.id,
        "nombre": build.name,
        "descripcion": build.description,
        "estado": build.status,
        "placa": {"ancho": build.baseplate_w, "fondo": build.baseplate_d},
        "num_pasos": len(pasos),
        "total_piezas": len(colocaciones),
        "pasos": pasos,
    }


def piezas_utilizables(session: Session, build_id: int, limite: int = 200) -> dict[str, Any]:
    """Paleta del diseñador: lo que hay disponible, con su forma ya resuelta."""
    build = requiere_montaje(session, build_id)
    listado = inventory.list_inventory(session, solo_disponibles=True, limit=limite)
    piezas = []
    for item in listado["items"]:
        forma = forma_de(item["pieza"], item["design_id"])
        reales = _medidas_con_origen(item["design_id"])
        relieve = perfil_de(item["pieza"], item["design_id"])
        piezas.append(
            {
                **item,
                "forma": forma.to_dict(),
                # Con malla se dibuja el molde de verdad; sin ella, su caja.
                "malla_3d": ldraw.hay_malla(item["design_id"]),
                "ajuste": ajuste_de_dibujo(forma, reales) if reales else None,
                "perfil": [[i, j, desde, hasta] for (i, j), (desde, hasta) in relieve.items()],
            }
        )
    # Primero lo que sabemos dibujar bien, y dentro de eso lo más abundante.
    piezas.sort(key=lambda p: (not p["malla_3d"], -p["disponible"]))
    return {"montaje_id": build.id, "total": listado["total"], "piezas": piezas}
