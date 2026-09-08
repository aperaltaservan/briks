"""Edición del modelo ya construido: mover, girar, duplicar, sustituir, pintar.

`designer.py` sabe poner una pieza en su sitio. Esto es lo otro que hace falta
para diseñar de verdad: rectificar lo que ya está puesto, y hacerlo con varias
piezas a la vez —que es como se trabaja cuando el modelo crece: «esta pared
entera una casilla a la derecha», «este trozo en rojo», «esto mismo pero
simétrico al otro lado».

Todas las operaciones trabajan sobre un grupo de colocaciones y comparten las
mismas reglas que colocar una pieza suelta:

  - el grupo se mueve rígido: las piezas que se mueven juntas no chocan entre
    sí, sólo contra el resto del modelo;
  - todo tiene que caber en la placa y no pisar a nadie;
  - lo que exige piezas nuevas (duplicar, sustituir) comprueba el inventario;
  - antes de tocar nada se guarda una foto para poder deshacer.

O entra el lote entero o no entra ninguno: media operación aplicada dejaría el
modelo en un estado que nadie pidió.
"""
from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..models import Build, Element, Placement
from . import builds as builds_service
from . import catalog, designer, geometry, historial, inventory
from .designer import DesignError

EJES = ("x", "z")


def _cuenta(n: int) -> str:
    """«1 pieza» / «4 piezas»: los mensajes se leen mejor bien escritos."""
    return f"{n} pieza" if n == 1 else f"{n} piezas"


# --------------------------------------------------------------------------
# El grupo sobre el que se trabaja
# --------------------------------------------------------------------------
def piezas_de(session: Session, build_id: int, ids: Iterable[int]) -> list[Placement]:
    """Las colocaciones indicadas, comprobando que existen y son de este montaje."""
    unicos = list(dict.fromkeys(int(i) for i in ids))
    if not unicos:
        raise DesignError("No has indicado ninguna pieza.")

    encontradas = {
        p.id: p
        for p in session.scalars(
            select(Placement)
            .where(Placement.id.in_(unicos))
            .options(joinedload(Placement.element).joinedload(Element.part))
        ).unique()
    }
    faltan = [i for i in unicos if i not in encontradas]
    if faltan:
        raise DesignError(
            f"No existen estas colocaciones: {', '.join(str(i) for i in faltan)}."
        )
    ajenas = [i for i in unicos if encontradas[i].build_id != int(build_id)]
    if ajenas:
        raise DesignError(
            f"Estas colocaciones no son del montaje {build_id}: "
            f"{', '.join(str(i) for i in ajenas)}."
        )
    return [encontradas[i] for i in unicos]


def caja_de(piezas: list[Placement]) -> tuple[int, int, int, int]:
    """Rectángulo que encierra al grupo, en studs: (x0, z0, x1, z1) con x1/z1 fuera."""
    x0 = min(p.x for p in piezas)
    z0 = min(p.z for p in piezas)
    x1 = 0
    z1 = 0
    for p in piezas:
        ancho, fondo = _forma(p).rotada(p.rotation)
        x1 = max(x1, p.x + ancho)
        z1 = max(z1, p.z + fondo)
    return x0, z0, x1, z1


def _forma(placement: Placement) -> geometry.Forma:
    return designer.forma_de(placement.element.part.name, placement.element.design_id)


def _destino(
    placement: Placement,
    x: int,
    y: int,
    z: int,
    rotacion: int,
    element: Element | None = None,
    etiqueta: str | None = None,
) -> dict[str, Any]:
    """Dónde acaba una pieza, con el molde con el que se la va a medir."""
    elemento = element or placement.element
    return {
        "placement": placement,
        "etiqueta": etiqueta or f"{elemento.part.name} (#{placement.id})",
        "element_id": elemento.element_id,
        "nombre": elemento.part.name,
        "design_id": elemento.design_id,
        "x": int(x),
        "y": int(y),
        "z": int(z),
        "rotacion": geometry.normalizar_rotacion(rotacion),
    }


def _comprobar_huecos(
    session: Session,
    build: Build,
    destinos: list[dict[str, Any]],
    ignorar: set[int],
) -> list[str]:
    """Valida que el grupo cabe donde quiere ir. Devuelve los avisos, no los errores.

    Las piezas de `ignorar` (las que se están moviendo) se sacan del modelo
    antes de mirar: si no, cualquier movimiento chocaría consigo mismo.
    """
    ocupado = designer.ocupacion(
        session, designer.colocaciones_de(session, build.id), ignorar=ignorar
    )
    avisos: list[str] = []

    for indice, d in enumerate(destinos, start=1):
        forma = designer.forma_de(d["nombre"], d["design_id"])
        ancho, fondo = forma.rotada(d["rotacion"])
        etiqueta = d["etiqueta"]

        if d["y"] < 0:
            raise DesignError(f"{etiqueta} no puede quedar por debajo de la placa.")
        if (
            d["x"] < 0
            or d["z"] < 0
            or d["x"] + ancho > build.baseplate_w
            or d["z"] + fondo > build.baseplate_d
        ):
            raise DesignError(
                f"{etiqueta} se saldría de la placa de "
                f"{build.baseplate_w}x{build.baseplate_d}: quedaría en "
                f"({d['x']},{d['z']}) ocupando {ancho}x{fondo}."
            )

        columnas = designer.columnas_de(
            d["nombre"], d["design_id"], d["x"], d["y"], d["z"], d["rotacion"]
        )
        for volumen in ocupado:
            if volumen.choca_con(columnas):
                raise DesignError(
                    f"{etiqueta} chocaría en ({d['x']},{d['y']},{d['z']}) con "
                    f"{volumen.etiqueta}."
                )

        huecas = designer.casillas_en_voladizo(ocupado, columnas)
        if huecas and len(huecas) == len(columnas):
            avisos.append(f"{etiqueta} queda al aire en ({d['x']},{d['y']},{d['z']}).")

        ocupado.append(designer.Volumen(columnas, etiqueta))
        d["columnas"] = columnas
        d["indice"] = indice
    return avisos


def _aplicar(session: Session, destinos: list[dict[str, Any]]) -> None:
    """Escribe el destino en cada colocación. Sólo se llama con todo ya validado."""
    for d in destinos:
        placement = d["placement"]
        placement.x, placement.y, placement.z = d["x"], d["y"], d["z"]
        placement.rotation = d["rotacion"]
        if placement.element_id != d["element_id"]:
            placement.element_id = d["element_id"]
            # Cambiar la clave no actualiza sola la relación: sin esto se
            # seguiría leyendo el elemento viejo al serializar la respuesta.
            session.expire(placement, ["element"])


def _respuesta(
    session: Session, build: Build, piezas: list[Placement], avisos: list[str], **extra: Any
) -> dict[str, Any]:
    session.flush()
    salida: dict[str, Any] = {
        "montaje_id": build.id,
        "afectadas": len(piezas),
        "piezas": [designer.pieza_a_dict(p, _forma(p)) for p in piezas],
        **extra,
    }
    if avisos:
        salida["avisos"] = avisos
    salida["historial"] = historial.estado(session, build.id)
    return salida


# --------------------------------------------------------------------------
# Mover, girar y reflejar
# --------------------------------------------------------------------------
def mover(
    session: Session,
    build_id: int,
    ids: Iterable[int],
    dx: int = 0,
    dy: int = 0,
    dz: int = 0,
    apoyar: bool = False,
) -> dict[str, Any]:
    """Desplaza el grupo entero, manteniendo las posiciones relativas.

    Con `apoyar` el grupo baja (o sube) lo justo para descansar sobre lo que
    haya debajo, en vez de moverse una altura fija: es lo que se espera al
    arrastrar un trozo del modelo sobre otro.
    """
    build = designer.requiere_montaje(session, build_id)
    piezas = piezas_de(session, build.id, ids)
    dx, dy, dz = int(dx), int(dy), int(dz)
    if not (dx or dy or dz or apoyar):
        raise DesignError("No has indicado ningún desplazamiento.")

    ignorar = {p.id for p in piezas}
    if apoyar:
        # El grupo se mueve rígido: baja lo que permita la pieza más justa.
        ocupado = designer.ocupacion(
            session, designer.colocaciones_de(session, build.id), ignorar=ignorar
        )
        dy = max(
            designer.altura_de_apoyo(
                ocupado,
                designer.columnas_de(
                    p.element.part.name,
                    p.element.design_id,
                    p.x + dx,
                    0,
                    p.z + dz,
                    p.rotation,
                ),
            )
            - p.y
            for p in piezas
        )

    destinos = [_destino(p, p.x + dx, p.y + dy, p.z + dz, p.rotation) for p in piezas]
    avisos = _comprobar_huecos(session, build, destinos, ignorar)

    historial.registrar(session, build.id, f"Mover {_cuenta(len(piezas))}")
    _aplicar(session, destinos)
    return _respuesta(
        session, build, piezas, avisos, desplazamiento={"dx": dx, "dy": dy, "dz": dz}
    )


def girar(
    session: Session, build_id: int, ids: Iterable[int], grados: int = 90
) -> dict[str, Any]:
    """Gira el grupo sobre sí mismo, alrededor de la esquina de su caja.

    Cada pieza gira también sobre su propio eje: el conjunto queda como si se
    hubiera girado el trozo de modelo entero, que es lo que se espera.
    """
    build = designer.requiere_montaje(session, build_id)
    piezas = piezas_de(session, build.id, ids)
    vueltas = (int(grados) // 90) % 4
    if int(grados) % 90:
        raise DesignError("Las piezas sólo giran de 90 en 90 grados.")
    if vueltas == 0:
        raise DesignError("Un giro de 0 grados no cambia nada.")

    # Se trabaja sobre copias hasta que todo esté validado.
    puestos = [{"p": p, "x": p.x, "z": p.z, "rot": p.rotation} for p in piezas]
    for _ in range(vueltas):
        x0 = min(c["x"] for c in puestos)
        z0 = min(c["z"] for c in puestos)
        z1 = max(
            c["z"] + _forma(c["p"]).rotada(c["rot"])[1] for c in puestos
        )
        nuevos = []
        for c in puestos:
            _, fondo = _forma(c["p"]).rotada(c["rot"])
            # Un cuarto de vuelta en la rejilla: (x, z) -> (z1 - z - fondo, x),
            # que es la misma transformación que se aplica a las casillas de
            # una pieza al girarla.
            nuevos.append(
                {
                    "p": c["p"],
                    "x": x0 + (z1 - c["z"] - fondo),
                    "z": z0 + (c["x"] - x0),
                    "rot": (c["rot"] + 90) % 360,
                }
            )
        puestos = nuevos

    destinos = [_destino(c["p"], c["x"], c["p"].y, c["z"], c["rot"]) for c in puestos]
    avisos = _comprobar_huecos(session, build, destinos, {p.id for p in piezas})

    historial.registrar(session, build.id, f"Girar {_cuenta(len(piezas))} {vueltas * 90}°")
    _aplicar(session, destinos)
    return _respuesta(session, build, piezas, avisos, giro=vueltas * 90)


def reflejar(
    session: Session, build_id: int, ids: Iterable[int], eje: str = "x"
) -> dict[str, Any]:
    """Refleja el grupo como en un espejo, sobre el eje X o el Z.

    Las piezas simétricas quedan perfectas. Una cuña o una curva no tienen
    espejo real —haría falta la pieza contraria—, así que se giran a la
    posición equivalente y se avisa: casi siempre habrá que rectificar alguna.
    """
    build = designer.requiere_montaje(session, build_id)
    piezas = piezas_de(session, build.id, ids)
    eje = (eje or "x").strip().lower()
    if eje not in EJES:
        raise DesignError("El eje del espejo tiene que ser 'x' o 'z'.")

    x0, z0, x1, z1 = caja_de(piezas)
    destinos = []
    asimetricas = 0
    for p in piezas:
        ancho, fondo = _forma(p).rotada(p.rotation)
        if eje == "x":
            # Espejo izquierda-derecha: la rotación se invierte.
            nuevo_x, nuevo_z = x0 + (x1 - p.x - ancho), p.z
            rotacion = (360 - p.rotation) % 360
        else:
            nuevo_x, nuevo_z = p.x, z0 + (z1 - p.z - fondo)
            rotacion = (180 - p.rotation) % 360
        if _forma(p).familia in (geometry.FAMILIA_SLOPE, geometry.FAMILIA_OTRA):
            asimetricas += 1
        destinos.append(_destino(p, nuevo_x, p.y, nuevo_z, rotacion))

    avisos = _comprobar_huecos(session, build, destinos, {p.id for p in piezas})
    if asimetricas:
        avisos.append(
            f"{_cuenta(asimetricas)} sin simetría propia (cuñas, curvas): en el "
            "modelo real harían falta sus piezas espejo."
        )

    historial.registrar(session, build.id, f"Reflejar {_cuenta(len(piezas))} en {eje}")
    _aplicar(session, destinos)
    return _respuesta(session, build, piezas, avisos, eje=eje)


# --------------------------------------------------------------------------
# Duplicar
# --------------------------------------------------------------------------
def duplicar(
    session: Session,
    build_id: int,
    ids: Iterable[int],
    dx: int = 0,
    dy: int = 0,
    dz: int = 0,
    paso_id: int | None = None,
    titulo_paso: str | None = None,
    permitir_faltantes: bool = False,
) -> dict[str, Any]:
    """Repite el grupo desplazado. Las copias gastan piezas del inventario."""
    build = designer.requiere_montaje(session, build_id)
    piezas = piezas_de(session, build.id, ids)
    dx, dy, dz = int(dx), int(dy), int(dz)
    if not (dx or dy or dz):
        raise DesignError(
            "Una copia en el mismo sitio chocaría con el original: indica un "
            "desplazamiento."
        )

    paso = designer.paso_destino(session, build, paso_id, titulo_paso)

    # Las copias no existen todavía: se validan contra el modelo entero.
    destinos = [
        _destino(
            p,
            p.x + dx,
            p.y + dy,
            p.z + dz,
            p.rotation,
            etiqueta=f"la copia de {p.element.part.name} (#{p.id})",
        )
        for p in piezas
    ]
    avisos = _comprobar_huecos(session, build, destinos, ignorar=set())

    extra: dict[str, int] = {}
    for d in destinos:
        extra[d["element_id"]] = extra.get(d["element_id"], 0) + 1
    faltantes = designer.comprobar_disponibilidad(
        session, build, designer.incremento_de_reserva(session, paso.id, extra)
    )
    if faltantes and not permitir_faltantes:
        raise DesignError(
            "No hay piezas suficientes para duplicar: "
            + "; ".join(
                f"{f['pieza']} ({f['color']}): necesitas {f['necesarias']}, "
                f"disponibles {f['disponibles']}"
                for f in faltantes
            )
        )

    historial.registrar(session, build.id, f"Duplicar {_cuenta(len(piezas))}")
    copias: list[Placement] = []
    for d in destinos:
        copia = Placement(
            build_id=build.id,
            step_id=paso.id,
            element_id=d["element_id"],
            x=d["x"],
            y=d["y"],
            z=d["z"],
            rotation=d["rotacion"],
        )
        session.add(copia)
        session.flush()
        designer.reserva_tras_colocar(session, paso.id, d["element_id"])
        copias.append(copia)

    resultado = _respuesta(
        session,
        build,
        copias,
        avisos,
        paso_id=paso.id,
        paso=paso.title,
        ids=[c.id for c in copias],
        desplazamiento={"dx": dx, "dy": dy, "dz": dz},
    )
    if faltantes:
        resultado["aviso_faltantes"] = faltantes
    return resultado


# --------------------------------------------------------------------------
# Sustituir la pieza o el color
# --------------------------------------------------------------------------
def _elemento_en_color(session: Session, element: Element, color: str) -> str:
    """El mismo molde en otro color. Es lo que hace el bote de pintura."""
    candidatos = catalog.search_elements(
        session, color=color, design_id=element.design_id, only_in_stock=True, limit=5
    )
    if not candidatos:
        sin_stock = catalog.search_elements(
            session, color=color, design_id=element.design_id, limit=5
        )
        if sin_stock:
            raise builds_service.PartResolutionError(
                f"Tienes {element.part.name} en {color} en el catálogo, pero sin "
                "unidades en inventario.",
                catalog.candidates_to_dicts(session, sin_stock),
            )
        raise DesignError(
            f"No hay ningún {element.part.name} en '{color}'. Los colores de "
            "los que tienes esa pieza salen en colores_de_pieza."
        )
    return candidatos[0].element.element_id


def sustituir(
    session: Session,
    build_id: int,
    ids: Iterable[int],
    element_id: str | None = None,
    color: str | None = None,
    descripcion: str | None = None,
    permitir_faltantes: bool = False,
) -> dict[str, Any]:
    """Cambia la pieza de unas colocaciones sin moverlas de sitio.

    Tres formas de decir por qué se cambia, de la más precisa a la más cómoda:
    `element_id` (molde y color exactos), `descripcion` ("ladrillo 2x4 rojo") o
    `color` a secas, que conserva el molde de cada pieza y sólo la repinta.

    El molde nuevo puede ocupar más sitio que el viejo, así que se comprueba
    otra vez que cabe.
    """
    build = designer.requiere_montaje(session, build_id)
    piezas = piezas_de(session, build.id, ids)

    destino_fijo: str | None = None
    if element_id:
        if session.get(Element, str(element_id).strip()) is None:
            raise DesignError(f"El elemento {element_id!r} no está en el catálogo.")
        destino_fijo = str(element_id).strip()
    elif descripcion:
        destino_fijo, _ = builds_service.resolve_part_spec(
            session, {"descripcion": descripcion, "color": color, "cantidad": 1}
        )
    elif not color:
        raise DesignError(
            "Di con qué sustituir: un element_id, una descripción o un color."
        )

    destinos = []
    cambios: list[tuple[Placement, str, str]] = []  # (pieza, antes, después)
    for p in piezas:
        nuevo_id = destino_fijo or _elemento_en_color(session, p.element, color)
        if nuevo_id == p.element_id:
            continue
        elemento = session.get(Element, nuevo_id)
        destinos.append(_destino(p, p.x, p.y, p.z, p.rotation, element=elemento))
        cambios.append((p, p.element_id, nuevo_id))

    if not cambios:
        raise DesignError("Esas piezas ya son las que pides: no hay nada que cambiar.")

    avisos = _comprobar_huecos(session, build, destinos, {p.id for p, _, _ in cambios})

    # El inventario: lo que se va se devuelve, lo que llega hay que tenerlo.
    por_paso: dict[int, dict[str, int]] = {}
    for placement, _antes, despues in cambios:
        por_paso.setdefault(placement.step_id, {})
        por_paso[placement.step_id][despues] = por_paso[placement.step_id].get(despues, 0) + 1

    necesario: dict[str, int] = {}
    for step_id, extra in por_paso.items():
        for element, unidades in designer.incremento_de_reserva(session, step_id, extra).items():
            necesario[element] = necesario.get(element, 0) + unidades
    faltantes = designer.comprobar_disponibilidad(session, build, necesario)
    if faltantes and not permitir_faltantes:
        raise DesignError(
            "No hay piezas suficientes para el cambio: "
            + "; ".join(
                f"{f['pieza']} ({f['color']}): necesitas {f['necesarias']}, "
                f"disponibles {f['disponibles']}"
                for f in faltantes
            )
        )

    historial.registrar(session, build.id, f"Sustituir {_cuenta(len(cambios))}")
    _aplicar(session, destinos)
    session.flush()
    # Primero se apunta lo nuevo y luego se suelta lo viejo, con las
    # colocaciones ya cambiadas: así ambas cuentas salen de lo que hay.
    for placement, _antes, despues in cambios:
        designer.reserva_tras_colocar(session, placement.step_id, despues)
    for placement, antes, _despues in cambios:
        designer.reserva_tras_quitar(session, placement.step_id, antes)

    resultado = _respuesta(
        session,
        build,
        [p for p, _, _ in cambios],
        avisos,
        cambios=[
            {"id": p.id, "antes": antes, "despues": despues} for p, antes, despues in cambios
        ],
    )
    if faltantes:
        resultado["aviso_faltantes"] = faltantes
    return resultado


def colores_de_pieza(session: Session, element_id: str, solo_disponibles: bool = True) -> dict[str, Any]:
    """Colores en los que tienes ese mismo molde. Es la paleta del bote de pintura."""
    element = session.get(Element, str(element_id).strip())
    if element is None:
        raise DesignError(f"El elemento {element_id!r} no está en el catálogo.")

    hermanos = list(
        session.scalars(
            select(Element)
            .where(Element.design_id == element.design_id)
            .options(joinedload(Element.color), joinedload(Element.part))
        ).unique()
    )
    disponible = inventory.availability(session, [e.element_id for e in hermanos])
    opciones = []
    for hermano in hermanos:
        datos = disponible[hermano.element_id]
        if solo_disponibles and datos["disponible"] <= 0 and hermano.element_id != element.element_id:
            continue
        opciones.append(
            {
                "element_id": hermano.element_id,
                "color": hermano.color.name,
                "color_hex": hermano.color.hex_code,
                "transparente": bool(hermano.color.is_transparent),
                "imagen": catalog.absolute_image_url(hermano.image_url),
                "actual": hermano.element_id == element.element_id,
                **datos,
            }
        )
    opciones.sort(key=lambda o: (not o["actual"], -o["disponible"], o["color"]))
    return {
        "element_id": element.element_id,
        "design_id": element.design_id,
        "pieza": element.part.name,
        "colores": opciones,
    }


# --------------------------------------------------------------------------
# Pasos y borrado en lote
# --------------------------------------------------------------------------
def cambiar_de_paso(
    session: Session,
    build_id: int,
    ids: Iterable[int],
    paso_id: int | None = None,
    titulo_paso: str | None = None,
) -> dict[str, Any]:
    """Lleva unas piezas a otro paso del montaje.

    No cambia nada en el modelo, sólo en las instrucciones: es la herramienta
    para reorganizar un manual que ha quedado desordenado. Las reservas se
    trasladan con las piezas, así que el inventario no se mueve.
    """
    build = designer.requiere_montaje(session, build_id)
    piezas = piezas_de(session, build.id, ids)
    paso = designer.paso_destino(session, build, paso_id, titulo_paso)

    mudanza = [p for p in piezas if p.step_id != paso.id]
    if not mudanza:
        raise DesignError(f"Esas piezas ya están en «{paso.title}».")

    origen = [(p.step_id, p.element_id) for p in mudanza]
    historial.registrar(session, build.id, f"Mover {_cuenta(len(mudanza))} a «{paso.title}»")
    for placement in mudanza:
        placement.step_id = paso.id
    session.flush()

    for element_id in {e for _, e in origen}:
        designer.reserva_tras_colocar(session, paso.id, element_id)
    for step_id, element_id in origen:
        designer.reserva_tras_quitar(session, step_id, element_id)

    return _respuesta(
        session, build, mudanza, [], paso_id=paso.id, paso=paso.title, movidas=len(mudanza)
    )


def quitar(session: Session, build_id: int, ids: Iterable[int]) -> dict[str, Any]:
    """Retira varias piezas de una vez. Todas vuelven al inventario disponible."""
    build = designer.requiere_montaje(session, build_id)
    piezas = piezas_de(session, build.id, ids)

    historial.registrar(session, build.id, f"Quitar {_cuenta(len(piezas))}")
    retiradas = [
        {
            "id": p.id,
            "element_id": p.element_id,
            "pieza": p.element.part.name,
            "paso_id": p.step_id,
        }
        for p in piezas
    ]
    for placement in piezas:
        session.delete(placement)
    session.flush()
    for datos in retiradas:
        designer.reserva_tras_quitar(session, datos["paso_id"], datos["element_id"])

    session.flush()
    return {
        "montaje_id": build.id,
        "eliminadas": len(retiradas),
        "detalle": retiradas,
        "historial": historial.estado(session, build.id),
    }


# --------------------------------------------------------------------------
# Buscar dentro del modelo
# --------------------------------------------------------------------------
def seleccionar(
    session: Session,
    build_id: int,
    element_id: str | None = None,
    design_id: str | None = None,
    color: str | None = None,
    pieza: str | None = None,
    paso_id: int | None = None,
    y: int | None = None,
    caja: dict[str, int] | None = None,
    limite: int = 500,
) -> dict[str, Any]:
    """Colocaciones que cumplen unos criterios: la selección, en datos.

    Es lo que permite decir «todas las placas rojas de la capa 3» o «lo que hay
    dentro de este rectángulo» sin tener que ir pieza por pieza; el resultado
    son ids listos para pasárselos a mover, sustituir o quitar.
    """
    build = designer.requiere_montaje(session, build_id)
    encontradas = []
    for placement in designer.colocaciones_de(session, build.id):
        elemento = placement.element
        if element_id and elemento.element_id != str(element_id).strip():
            continue
        if design_id and elemento.design_id != str(design_id).strip():
            continue
        if color and color.strip().lower() not in elemento.color.name.lower():
            continue
        if pieza and pieza.strip().lower() not in elemento.part.name.lower():
            continue
        if paso_id and placement.step_id != int(paso_id):
            continue
        forma = _forma(placement)
        if y is not None and not (placement.y <= int(y) < placement.y + forma.alto):
            continue
        if caja:
            ancho, fondo = forma.rotada(placement.rotation)
            if (
                placement.x + ancho <= int(caja.get("x0", 0))
                or placement.x >= int(caja.get("x1", build.baseplate_w))
                or placement.z + fondo <= int(caja.get("z0", 0))
                or placement.z >= int(caja.get("z1", build.baseplate_d))
            ):
                continue
        encontradas.append(placement)

    resumen: dict[str, dict[str, Any]] = {}
    for placement in encontradas:
        entrada = resumen.setdefault(
            placement.element_id,
            {
                "element_id": placement.element_id,
                "pieza": placement.element.part.name,
                "color": placement.element.color.name,
                "cantidad": 0,
            },
        )
        entrada["cantidad"] += 1

    return {
        "montaje_id": build.id,
        "total": len(encontradas),
        "ids": [p.id for p in encontradas[:limite]],
        "resumen": sorted(resumen.values(), key=lambda r: -r["cantidad"]),
        "piezas": [
            designer.pieza_a_dict(p, _forma(p)) for p in encontradas[: min(limite, 200)]
        ],
    }
