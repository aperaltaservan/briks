"""Servidor MCP: el motor de la aplicación.

Todo lo que se puede hacer desde la web se puede hacer desde el chat a través de
estas herramientas, porque ambas usan exactamente los mismos servicios.

Se expone por dos transportes:
  - HTTP  : montado en /mcp dentro de la app FastAPI (para clientes remotos).
  - stdio : `python -m app.mcp_stdio` (para Claude Desktop / Claude Code).
"""
from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from .db import session_scope
from .models import Element
from .services import builds, catalog, designer, importers, inventory, recognition
from .services.builds import BuildError, PartResolutionError
from .services.designer import DesignError
from .services.inventory import InventoryError
from .services.recognition import RecognitionError

INSTRUCCIONES = """
Motor de inventario y montajes LEGO.

Conceptos que conviene tener claros antes de llamar a nada:
  - DesignID  = el molde (3021 = PLATE 2X3). ElementID = molde + color (302126).
    El inventario se lleva por ElementID.
  - Los colores usan la nomenclatura oficial LEGO: "Brick Yellow" es el beige y
    "Medium Stone Grey" el gris claro. Puedes escribir en español ("placa 2x4
    roja"): las herramientas traducen.
  - Importar un set NO añade piezas al inventario; sólo registra de qué se
    compone. Para tenerlas, usa inventariar_set.
  - Un montaje activo reserva sus piezas: "disponible" = existencias - reservado.
  - Un set ya inventariado no se vuelve a inventariar sin más: la herramienta
    avisa y pide confirmar=true. Confírmalo sólo si de verdad hay otra copia.

Los montajes tienen modelo 3D: cada pieza se coloca en unas coordenadas de la
placa base (x, z en studs; y en placas, tres por ladrillo) y pertenece a un
paso, que es lo que convierte el montaje en instrucciones visuales. Para
construir usa colocar_piezas; sin 'y' la pieza se apoya sola sobre lo que haya
debajo. ver_modelo con mapa=true devuelve la planta de cada capa en texto: es
la forma de comprobar lo que llevas hecho sin ver la pantalla.

Para inventariar desde una FOTO: mira tú la imagen, describe cada pieza con su
cantidad y color, y llama a registrar_piezas_detectadas. Lo que quede como
pendiente hay que resolverlo (resolver_reconocimiento) y después confirmar con
confirmar_reconocimiento, que es el único paso que suma al inventario.
""".strip()

mcp = MCPServer(
    name="lego-inventario",
    title="Inventario y montajes LEGO",
    instructions=INSTRUCCIONES,
    version="0.1.0",
)


def _error(exc: Exception) -> dict[str, Any]:
    """Convierte un error de negocio en respuesta estructurada.

    Devolvemos el error como dato (no como excepción) para que el chat pueda
    reaccionar: sobre todo cuando trae candidatos que permiten desambiguar.
    """
    salida: dict[str, Any] = {"ok": False, "error": str(exc)}
    candidatos = getattr(exc, "candidatos", None)
    if candidatos:
        salida["candidatos"] = candidatos
        salida["sugerencia"] = (
            "Elige uno de los candidatos y vuelve a llamar indicando su element_id."
        )
    if getattr(exc, "requiere_confirmacion", False):
        salida["requiere_confirmacion"] = True
        salida["detalle"] = getattr(exc, "detalle", None)
        salida["sugerencia"] = (
            "Pregunta a la persona antes de seguir. Si confirma, repite la "
            "llamada con confirmar=true."
        )
    return salida


ERRORES_NEGOCIO = (
    BuildError,
    InventoryError,
    RecognitionError,
    ValueError,
    PartResolutionError,
    DesignError,
)


# ==========================================================================
# Panorama general
# ==========================================================================
@mcp.tool(
    description=(
        "Panorama del inventario: piezas totales, disponibles, reservadas en "
        "montajes, desglose por color y categoría, y tamaño del catálogo. "
        "Empieza por aquí para saber con qué se cuenta."
    )
)
def resumen_inventario() -> dict[str, Any]:
    with session_scope() as s:
        return {"ok": True, **inventory.inventory_summary(s)}


# ==========================================================================
# Catálogo y búsqueda
# ==========================================================================
@mcp.tool(
    description=(
        "Busca piezas por descripción en lenguaje natural ('placa 2x4 roja', "
        "'loseta 1x1'). Devuelve ElementIDs concretos con cuántas hay en "
        "inventario. Úsala siempre que necesites identificar una pieza antes "
        "de inventariarla o usarla en un montaje."
    )
)
def buscar_piezas(
    descripcion: str = "",
    color: str = "",
    categoria: str = "",
    design_id: str = "",
    solo_en_inventario: bool = False,
    limite: int = 10,
) -> dict[str, Any]:
    with session_scope() as s:
        candidatos = catalog.search_elements(
            s,
            query=descripcion or None,
            color=color or None,
            category=categoria or None,
            design_id=design_id or None,
            only_in_stock=solo_en_inventario,
            limit=max(1, min(limite, 50)),
        )
        resultados = catalog.candidates_to_dicts(s, candidatos)
        return {
            "ok": True,
            "consulta": {"descripcion": descripcion, "color": color, "categoria": categoria},
            "encontradas": len(resultados),
            "piezas": resultados,
        }


@mcp.tool(
    description=(
        "Lista el inventario con filtros opcionales (descripción, color, "
        "categoría, ubicación). Muestra existencias, reservado y disponible."
    )
)
def consultar_inventario(
    descripcion: str = "",
    color: str = "",
    categoria: str = "",
    ubicacion: str = "",
    solo_disponibles: bool = False,
    limite: int = 30,
) -> dict[str, Any]:
    with session_scope() as s:
        return {
            "ok": True,
            **inventory.list_inventory(
                s,
                query=descripcion or None,
                color=color or None,
                category=categoria or None,
                location=ubicacion or None,
                solo_disponibles=solo_disponibles,
                limit=max(1, min(limite, 200)),
            ),
        }


@mcp.tool(description="Colores disponibles en el catálogo, con cuántas piezas hay de cada uno.")
def listar_colores() -> dict[str, Any]:
    with session_scope() as s:
        resumen = inventory.inventory_summary(s)
        return {"ok": True, "colores": resumen["por_color"]}


# ==========================================================================
# Sets
# ==========================================================================
@mcp.tool(
    description=(
        "Importa el inventario de un set desde el CSV de LEGO.com (columnas "
        "SetNumber, ElementID, Qty, Colour, Category, DesignID, ElementName, "
        "ImageURL). Registra el set y amplía el catálogo, pero NO suma piezas "
        "al inventario: para eso usa inventariar_set."
    )
)
def importar_set_csv(
    contenido_csv: str, numero_set: str = "", nombre_set: str = ""
) -> dict[str, Any]:
    try:
        with session_scope() as s:
            resultado = importers.import_set_csv(
                s,
                contenido_csv,
                set_number=numero_set or None,
                set_name=nombre_set or None,
            )
            return {"ok": True, **resultado.to_dict()}
    except ERRORES_NEGOCIO as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Añade al inventario todas las piezas de un set ya importado. Usa "
        "'veces' si tienes el mismo set repetido. Este SÍ modifica el "
        "inventario. Si el set YA estaba inventariado no hace nada y devuelve "
        "requiere_confirmacion: avisa a la persona de que volver a hacerlo "
        "duplicaría las piezas y sólo repite con confirmar=true si lo confirma."
    )
)
def inventariar_set(
    numero_set: str, veces: int = 1, ubicacion: str = "", confirmar: bool = False
) -> dict[str, Any]:
    try:
        with session_scope() as s:
            return {
                "ok": True,
                **inventory.add_set_to_inventory(
                    s, numero_set, veces, ubicacion, confirmar=confirmar
                ),
            }
    except ERRORES_NEGOCIO as exc:
        return _error(exc)


@mcp.tool(description="Lista los sets importados, con su número de piezas y si están inventariados.")
def listar_sets() -> dict[str, Any]:
    from sqlalchemy import func, select

    from .models import LegoSet, SetPart

    with session_scope() as s:
        salida = []
        for lego_set in s.scalars(select(LegoSet).order_by(LegoSet.set_number)):
            piezas = (
                s.scalar(
                    select(func.sum(SetPart.quantity)).where(
                        SetPart.set_number == lego_set.set_number
                    )
                )
                or 0
            )
            referencias = (
                s.scalar(
                    select(func.count(SetPart.id)).where(SetPart.set_number == lego_set.set_number)
                )
                or 0
            )
            historial = inventory.historial_de_set(s, lego_set.set_number)
            salida.append(
                {
                    "set": lego_set.set_number,
                    "nombre": lego_set.name,
                    "piezas": int(piezas),
                    "referencias": int(referencias),
                    "inventariado": lego_set.owned,
                    "piezas_ya_inventariadas": historial["piezas_anadidas"],
                    "ultima_vez": historial["ultima_vez"],
                }
            )
        return {"ok": True, "total": len(salida), "sets": salida}


@mcp.tool(description="Detalle de las piezas que componen un set importado.")
def piezas_de_set(numero_set: str, limite: int = 100) -> dict[str, Any]:
    from sqlalchemy import select

    from .models import LegoSet, SetPart

    numero = numero_set.strip()
    if "-" not in numero:
        numero = f"{numero}-1"

    with session_scope() as s:
        lego_set = s.get(LegoSet, numero)
        if lego_set is None:
            return {"ok": False, "error": f"El set {numero} no está importado."}

        lineas = list(s.scalars(select(SetPart).where(SetPart.set_number == numero)))
        ids = [l.element_id for l in lineas]
        disponibilidad = inventory.availability(s, ids)
        piezas = []
        for linea in lineas[: max(1, limite)]:
            element = s.get(Element, linea.element_id)
            piezas.append(
                {
                    "element_id": linea.element_id,
                    "pieza": element.part.name if element else linea.element_id,
                    "color": element.color.name if element else None,
                    "cantidad_en_set": linea.quantity,
                    **disponibilidad.get(linea.element_id, {}),
                }
            )
        piezas.sort(key=lambda p: -p["cantidad_en_set"])
        return {
            "ok": True,
            "set": numero,
            "nombre": lego_set.name,
            "referencias": len(lineas),
            "piezas_totales": sum(l.quantity for l in lineas),
            "piezas": piezas,
        }


# ==========================================================================
# Ajustes de inventario
# ==========================================================================
@mcp.tool(
    description=(
        "Ajusta las existencias de una pieza. modo='sumar' añade (usa negativo "
        "para retirar), modo='fijar' establece el recuento exacto. Identifica la "
        "pieza por element_id; si no lo sabes, búscalo antes con buscar_piezas."
    )
)
def ajustar_inventario(
    element_id: str,
    cantidad: int,
    modo: str = "sumar",
    ubicacion: str = "",
    motivo: str = "manual",
) -> dict[str, Any]:
    try:
        with session_scope() as s:
            if modo.strip().lower() in ("fijar", "set", "absoluto"):
                item = inventory.set_stock(s, element_id, cantidad, ubicacion, reason=motivo)
            else:
                item = inventory.add_stock(s, element_id, cantidad, ubicacion, reason=motivo)
            return {"ok": True, **item}
    except ERRORES_NEGOCIO as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Da de alta en el catálogo una pieza que no existe todavía (molde + "
        "color) y opcionalmente la añade al inventario. Útil para piezas "
        "sueltas que no vienen de ningún set importado."
    )
)
def alta_pieza_manual(
    design_id: str,
    nombre_pieza: str,
    color: str,
    element_id: str = "",
    categoria: str = "",
    cantidad: int = 0,
    ubicacion: str = "",
) -> dict[str, Any]:
    try:
        with session_scope() as s:
            color_obj = catalog.get_or_create_color(s, color)
            catalog.get_or_create_part(s, design_id, nombre_pieza, categoria or None)
            # Sin ElementID oficial fabricamos uno estable a partir del molde y el color.
            eid = element_id.strip() or f"{design_id.strip()}-{color_obj.id}"
            catalog.get_or_create_element(s, eid, design_id.strip(), color_obj.id)
            resultado: dict[str, Any] = {
                "ok": True,
                "element_id": eid,
                "design_id": design_id.strip(),
                "pieza": nombre_pieza,
                "color": color_obj.name,
                "element_id_generado": not bool(element_id.strip()),
            }
            if cantidad:
                resultado["inventario"] = inventory.add_stock(
                    s, eid, cantidad, ubicacion, reason="manual"
                )
            return resultado
    except ERRORES_NEGOCIO as exc:
        return _error(exc)


@mcp.tool(description="Últimos movimientos de inventario, para auditar de dónde salió cada pieza.")
def historial_movimientos(element_id: str = "", limite: int = 30) -> dict[str, Any]:
    with session_scope() as s:
        return {
            "ok": True,
            "movimientos": inventory.movements(s, element_id or None, max(1, min(limite, 200))),
        }


# ==========================================================================
# Inventariado por imagen
# ==========================================================================
@mcp.tool(
    description=(
        "Registra las piezas que TÚ has identificado mirando una foto. Pasa una "
        "lista de detecciones, cada una como "
        "{'descripcion': 'placa 2x4', 'color': 'rojo', 'cantidad': 3}. "
        "Opcionalmente 'element_id' si lo conoces con certeza. "
        "Cada detección se casa con el catálogo: las claras quedan resueltas y "
        "las dudosas pendientes con candidatos. NO toca el inventario todavía; "
        "hay que confirmar después con confirmar_reconocimiento."
    )
)
def registrar_piezas_detectadas(
    detecciones: list[dict[str, Any]],
    imagen_base64: str = "",
    notas: str = "",
) -> dict[str, Any]:
    try:
        with session_scope() as s:
            return {
                "ok": True,
                **recognition.crear_sesion(
                    s,
                    detecciones,
                    imagen_base64=imagen_base64 or None,
                    notas=notas or None,
                ),
            }
    except ERRORES_NEGOCIO as exc:
        return _error(exc)


@mcp.tool(description="Estado de una sesión de reconocimiento: qué está resuelto y qué queda pendiente.")
def ver_reconocimiento(sesion_id: int) -> dict[str, Any]:
    try:
        with session_scope() as s:
            return {"ok": True, **recognition.detalle_sesion(s, sesion_id)}
    except ERRORES_NEGOCIO as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Resuelve las detecciones dudosas de una sesión. Cada resolución es "
        "{'item_id': 3, 'element_id': '302126'} o {'item_id': 3, 'descartar': true}; "
        "puedes incluir 'cantidad' para corregir el recuento."
    )
)
def resolver_reconocimiento(
    sesion_id: int, resoluciones: list[dict[str, Any]]
) -> dict[str, Any]:
    try:
        with session_scope() as s:
            return {"ok": True, **recognition.resolver_items(s, sesion_id, resoluciones)}
    except ERRORES_NEGOCIO as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Aplica al inventario las piezas resueltas de una sesión de "
        "reconocimiento. Es el paso que SÍ suma piezas. Por defecto ignora las "
        "que sigan pendientes."
    )
)
def confirmar_reconocimiento(
    sesion_id: int, ubicacion: str = "", ignorar_pendientes: bool = True
) -> dict[str, Any]:
    try:
        with session_scope() as s:
            return {
                "ok": True,
                **recognition.confirmar_sesion(
                    s, sesion_id, ubicacion=ubicacion, solo_resueltas=ignorar_pendientes
                ),
            }
    except ERRORES_NEGOCIO as exc:
        return _error(exc)


@mcp.tool(description="Lista las sesiones de reconocimiento recientes.")
def listar_reconocimientos(estado: str = "", limite: int = 20) -> dict[str, Any]:
    with session_scope() as s:
        return {
            "ok": True,
            "sesiones": recognition.listar_sesiones(s, estado or None, max(1, min(limite, 100))),
        }


@mcp.tool(description="Descarta una sesión de reconocimiento sin aplicarla al inventario.")
def descartar_reconocimiento(sesion_id: int) -> dict[str, Any]:
    try:
        with session_scope() as s:
            return {"ok": True, **recognition.descartar_sesion(s, sesion_id)}
    except ERRORES_NEGOCIO as exc:
        return _error(exc)


# ==========================================================================
# Montajes
# ==========================================================================
@mcp.tool(description="Crea un montaje vacío al que después se le añaden pasos en orden.")
def crear_montaje(nombre: str, descripcion: str = "") -> dict[str, Any]:
    try:
        with session_scope() as s:
            return {"ok": True, **builds.create_build(s, nombre, descripcion or None)}
    except ERRORES_NEGOCIO as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Añade un paso al montaje. Las piezas se indican como lista de "
        "{'descripcion': 'placa 2x4', 'color': 'rojo', 'cantidad': 2} o "
        "{'element_id': '302126', 'cantidad': 2}. Comprueba que hay piezas "
        "disponibles y falla si no llegan, salvo que permitir_faltantes sea true. "
        "Si una descripción es ambigua devuelve los candidatos para elegir."
    )
)
def anadir_paso(
    montaje_id: int,
    titulo: str,
    piezas: list[dict[str, Any]] | None = None,
    instruccion: str = "",
    posicion: int = 0,
    permitir_faltantes: bool = False,
) -> dict[str, Any]:
    try:
        with session_scope() as s:
            return {
                "ok": True,
                **builds.add_step(
                    s,
                    montaje_id,
                    titulo,
                    parts=piezas or [],
                    instruction=instruccion or None,
                    position=posicion if posicion and posicion > 0 else None,
                    permitir_faltantes=permitir_faltantes,
                ),
            }
    except ERRORES_NEGOCIO as exc:
        return _error(exc)


@mcp.tool(description="Detalle completo de un montaje: sus pasos en orden y las piezas de cada uno.")
def ver_montaje(montaje_id: int) -> dict[str, Any]:
    try:
        with session_scope() as s:
            return {"ok": True, **builds.build_detail(s, montaje_id)}
    except ERRORES_NEGOCIO as exc:
        return _error(exc)


@mcp.tool(description="Lista los montajes, opcionalmente filtrando por estado.")
def listar_montajes(estado: str = "") -> dict[str, Any]:
    with session_scope() as s:
        return {"ok": True, "montajes": builds.list_builds(s, estado or None)}


@mcp.tool(description="Marca un paso como 'hecho' o 'pendiente'. Al completarlos todos el montaje pasa a completado.")
def marcar_paso(paso_id: int, estado: str = "hecho") -> dict[str, Any]:
    try:
        with session_scope() as s:
            return {"ok": True, **builds.set_step_status(s, paso_id, estado)}
    except ERRORES_NEGOCIO as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Corrige un paso ya creado: su título, su instrucción o sus piezas. "
        "Sólo cambia lo que indiques; lo que dejes vacío se queda como está. "
        "Si pasas 'piezas', sustituyen por completo a las anteriores."
    )
)
def editar_paso(
    paso_id: int,
    titulo: str = "",
    instruccion: str = "",
    piezas: list[dict[str, Any]] | None = None,
    permitir_faltantes: bool = False,
) -> dict[str, Any]:
    try:
        with session_scope() as s:
            return {
                "ok": True,
                **builds.update_step(
                    s,
                    paso_id,
                    title=titulo or None,
                    instruction=instruccion or None,
                    parts=piezas,
                    permitir_faltantes=permitir_faltantes,
                ),
            }
    except ERRORES_NEGOCIO as exc:
        return _error(exc)


@mcp.tool(description="Elimina un paso del montaje y recompacta la numeración de los siguientes.")
def eliminar_paso(paso_id: int) -> dict[str, Any]:
    try:
        with session_scope() as s:
            return {"ok": True, **builds.remove_step(s, paso_id)}
    except ERRORES_NEGOCIO as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Cambia el estado del montaje: planificado, en_progreso, completado o "
        "desmontado. 'desmontado' libera sus piezas y vuelven a estar disponibles."
    )
)
def cambiar_estado_montaje(montaje_id: int, estado: str) -> dict[str, Any]:
    try:
        with session_scope() as s:
            return {"ok": True, **builds.set_build_status(s, montaje_id, estado)}
    except ERRORES_NEGOCIO as exc:
        return _error(exc)


@mcp.tool(description="Elimina un montaje por completo y libera sus piezas.")
def eliminar_montaje(montaje_id: int) -> dict[str, Any]:
    try:
        with session_scope() as s:
            return {"ok": True, **builds.delete_build(s, montaje_id)}
    except ERRORES_NEGOCIO as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Comprueba si un montaje es viable con el inventario actual y detalla "
        "qué piezas faltarían. No cuenta las reservas del propio montaje."
    )
)
def comprobar_montaje(montaje_id: int) -> dict[str, Any]:
    try:
        with session_scope() as s:
            return {"ok": True, **builds.check_build(s, montaje_id)}
    except ERRORES_NEGOCIO as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "De los sets importados, cuáles se pueden montar ahora mismo con las "
        "piezas disponibles y, si no, qué porcentaje se cubre y qué falta."
    )
)
def que_puedo_montar(cobertura_minima: float = 0.0) -> dict[str, Any]:
    with session_scope() as s:
        sets = builds.buildable_sets(s, umbral=max(0.0, min(cobertura_minima, 1.0)))
        return {
            "ok": True,
            "completos": [x for x in sets if x["completo"]],
            "parciales": [x for x in sets if not x["completo"]],
        }


# ==========================================================================
# Diseñador 3D
# ==========================================================================
@mcp.tool(
    description=(
        "Coloca piezas en el modelo 3D de un montaje, todas dentro del mismo "
        "paso. Cada pieza es {'element_id': '302126', 'x': 4, 'z': 2} o "
        "{'descripcion': 'placa 2x4', 'color': 'rojo', 'x': 4, 'z': 2}, más "
        "'y' (altura en placas) y 'rotacion' (0, 90, 180 o 270) opcionales. "
        "Sin 'y' la pieza se apoya sobre lo que haya debajo, que es lo normal. "
        "Coordenadas: x y z en studs desde la esquina de la placa; el eje y "
        "sube en placas y un ladrillo son 3. Con 'titulo_paso' se crea un paso "
        "nuevo; sin él las piezas van al último. Comprueba choques, límites de "
        "la placa y disponibilidad real: o entran todas o no entra ninguna."
    )
)
def colocar_piezas(
    montaje_id: int,
    piezas: list[dict[str, Any]],
    paso_id: int = 0,
    titulo_paso: str = "",
    permitir_faltantes: bool = False,
) -> dict[str, Any]:
    try:
        with session_scope() as s:
            return {
                "ok": True,
                **designer.colocar_piezas(
                    s,
                    montaje_id,
                    piezas,
                    paso_id=paso_id or None,
                    titulo_paso=titulo_paso or None,
                    permitir_faltantes=permitir_faltantes,
                ),
            }
    except ERRORES_NEGOCIO as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Cambia de sitio una pieza ya colocada. Se identifica por el id de la "
        "colocación (el que devuelve colocar_piezas o ver_modelo), no por el "
        "element_id. Lo que no indiques se queda como estaba."
    )
)
def mover_pieza(
    colocacion_id: int, x: int | None = None, z: int | None = None,
    y: int | None = None, rotacion: int | None = None,
) -> dict[str, Any]:
    try:
        with session_scope() as s:
            return {"ok": True, **designer.mover_pieza(s, colocacion_id, x=x, z=z, y=y, rotacion=rotacion)}
    except ERRORES_NEGOCIO as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Quita del modelo una pieza colocada y devuelve su unidad al "
        "inventario disponible. Se identifica por el id de la colocación."
    )
)
def quitar_pieza(colocacion_id: int) -> dict[str, Any]:
    try:
        with session_scope() as s:
            return {"ok": True, **designer.quitar_pieza(s, colocacion_id)}
    except ERRORES_NEGOCIO as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Vacía el modelo 3D de un montaje, o sólo las piezas de un paso si se "
        "indica paso_id. Las piezas vuelven a estar disponibles."
    )
)
def vaciar_modelo(montaje_id: int, paso_id: int = 0) -> dict[str, Any]:
    try:
        with session_scope() as s:
            return {"ok": True, **designer.vaciar(s, montaje_id, paso_id or None)}
    except ERRORES_NEGOCIO as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "El modelo 3D de un montaje: placa base, pasos y todas las piezas con "
        "sus coordenadas y su forma. Con mapa=true añade la planta de cada "
        "capa dibujada en texto, que es la forma de comprobar cómo va quedando "
        "el modelo desde una conversación."
    )
)
def ver_modelo(montaje_id: int, mapa: bool = True) -> dict[str, Any]:
    try:
        with session_scope() as s:
            return {"ok": True, **designer.modelo(s, montaje_id, con_mapa=mapa)}
    except ERRORES_NEGOCIO as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "El montaje convertido en manual de instrucciones: para cada paso, qué "
        "piezas entran (con su imagen y cantidad, como el recuadro de los "
        "manuales), dónde va cada una y cuántas había ya montadas."
    )
)
def instrucciones_montaje(montaje_id: int) -> dict[str, Any]:
    try:
        with session_scope() as s:
            return {"ok": True, **designer.instrucciones(s, montaje_id)}
    except ERRORES_NEGOCIO as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Piezas disponibles para diseñar, con su forma ya resuelta (ancho, "
        "fondo y alto en unidades LEGO). Útil antes de colocar nada: dice con "
        "qué se puede contar y cómo de grande es cada pieza."
    )
)
def paleta_de_piezas(montaje_id: int, limite: int = 60) -> dict[str, Any]:
    try:
        with session_scope() as s:
            return {"ok": True, **designer.piezas_utilizables(s, montaje_id, limite)}
    except ERRORES_NEGOCIO as exc:
        return _error(exc)


@mcp.tool(
    description=(
        "Cambia el tamaño de la placa base del montaje, en studs (entre 8 y "
        "96 por lado). No deja encogerla si alguna pieza quedaría fuera."
    )
)
def configurar_placa(montaje_id: int, ancho: int = 32, fondo: int = 32) -> dict[str, Any]:
    try:
        with session_scope() as s:
            return {"ok": True, **designer.configurar_placa(s, montaje_id, ancho, fondo)}
    except ERRORES_NEGOCIO as exc:
        return _error(exc)
