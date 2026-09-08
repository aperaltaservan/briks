"""API REST.

Es una capa fina sobre los mismos servicios que usa el MCP: la web y el chat
ven exactamente el mismo estado, sin lógica duplicada.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Element, LegoSet, SetPart
from ..services import (
    builds,
    catalog,
    designer,
    editor,
    historial,
    importers,
    inventory,
    ldraw,
    recognition,
)
from ..services.builds import BuildError
from ..services.inventory import InventoryError
from ..services.recognition import RecognitionError

router = APIRouter(prefix="/api", tags=["lego"])

ERRORES = (BuildError, InventoryError, RecognitionError, ValueError)


def _ejecutar(funcion, *args, **kwargs):
    """Traduce los errores de negocio a respuestas HTTP con mensaje útil.

    Un error que sólo pide confirmación (reinventariar un set) sale como 409:
    no es un fallo de la petición, es una pregunta que hay que responder.
    """
    try:
        return funcion(*args, **kwargs)
    except ERRORES as exc:
        detalle: dict[str, Any] = {"error": str(exc)}
        candidatos = getattr(exc, "candidatos", None)
        if candidatos:
            detalle["candidatos"] = candidatos
        if getattr(exc, "requiere_confirmacion", False):
            detalle["requiere_confirmacion"] = True
            detalle["detalle"] = getattr(exc, "detalle", None)
            raise HTTPException(status_code=409, detail=detalle) from exc
        raise HTTPException(status_code=400, detail=detalle) from exc


# --------------------------------------------------------------------------
# Esquemas de entrada
# --------------------------------------------------------------------------
class AjusteInventario(BaseModel):
    element_id: str
    cantidad: int
    modo: str = Field(default="sumar", description="'sumar' o 'fijar'")
    ubicacion: str = ""
    motivo: str = "manual"


class ImportacionCSV(BaseModel):
    contenido_csv: str
    numero_set: str | None = None
    nombre_set: str | None = None


class InventariarSet(BaseModel):
    veces: int = 1
    ubicacion: str = ""
    # Sin confirmar, un set ya inventariado se rechaza con un 409 en vez de
    # duplicar las existencias.
    confirmar: bool = False


class NuevoMontaje(BaseModel):
    nombre: str
    descripcion: str | None = None


class NuevoPaso(BaseModel):
    titulo: str
    piezas: list[dict[str, Any]] = Field(default_factory=list)
    instruccion: str | None = None
    posicion: int | None = None
    permitir_faltantes: bool = False


class CambioEstado(BaseModel):
    estado: str


class EdicionPaso(BaseModel):
    titulo: str | None = None
    instruccion: str | None = None
    piezas: list[dict[str, Any]] | None = None
    permitir_faltantes: bool = False


class DeteccionesImagen(BaseModel):
    detecciones: list[dict[str, Any]]
    imagen_base64: str | None = None
    notas: str | None = None


class ResolucionesReconocimiento(BaseModel):
    resoluciones: list[dict[str, Any]]


class ConfirmacionReconocimiento(BaseModel):
    ubicacion: str = ""
    ignorar_pendientes: bool = True


class ColocacionPiezas(BaseModel):
    piezas: list[dict[str, Any]]
    paso_id: int | None = None
    titulo_paso: str | None = None
    permitir_faltantes: bool = False


class MovimientoPieza(BaseModel):
    x: int | None = None
    y: int | None = None
    z: int | None = None
    rotacion: int | None = None


class TamanoPlaca(BaseModel):
    ancho: int
    fondo: int


# Edición en lote: todas las operaciones del editor trabajan sobre una
# selección de colocaciones, así que comparten la lista de ids.
class Seleccion(BaseModel):
    ids: list[int]


class MovimientoLote(Seleccion):
    dx: int = 0
    dy: int = 0
    dz: int = 0
    # Con apoyar, el grupo cae sobre lo que haya debajo en vez de subir o bajar
    # una altura fija.
    apoyar: bool = False


class DuplicadoLote(Seleccion):
    dx: int = 0
    dy: int = 0
    dz: int = 0
    paso_id: int | None = None
    titulo_paso: str | None = None
    permitir_faltantes: bool = False


class GiroLote(Seleccion):
    grados: int = 90


class EspejoLote(Seleccion):
    eje: str = "x"


class SustitucionLote(Seleccion):
    element_id: str | None = None
    color: str | None = None
    descripcion: str | None = None
    permitir_faltantes: bool = False


class PasoDeLote(Seleccion):
    paso_id: int | None = None
    titulo_paso: str | None = None


# --------------------------------------------------------------------------
# Panorama e inventario
# --------------------------------------------------------------------------
@router.get("/resumen")
def resumen(session: Session = Depends(get_session)) -> dict[str, Any]:
    return inventory.inventory_summary(session)


@router.get("/inventario")
def listar_inventario(
    descripcion: str = "",
    color: str = "",
    categoria: str = "",
    ubicacion: str = "",
    solo_disponibles: bool = False,
    limite: int = 60,
    offset: int = 0,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    return inventory.list_inventory(
        session,
        query=descripcion or None,
        color=color or None,
        category=categoria or None,
        location=ubicacion or None,
        solo_disponibles=solo_disponibles,
        limit=max(1, min(limite, 500)),
        offset=max(0, offset),
    )


@router.post("/inventario/ajustar")
def ajustar(ajuste: AjusteInventario, session: Session = Depends(get_session)) -> dict[str, Any]:
    if ajuste.modo.lower() in ("fijar", "set", "absoluto"):
        resultado = _ejecutar(
            inventory.set_stock,
            session,
            ajuste.element_id,
            ajuste.cantidad,
            ajuste.ubicacion,
            reason=ajuste.motivo,
        )
    else:
        resultado = _ejecutar(
            inventory.add_stock,
            session,
            ajuste.element_id,
            ajuste.cantidad,
            ajuste.ubicacion,
            reason=ajuste.motivo,
        )
    session.commit()
    return resultado


@router.get("/piezas/buscar")
def buscar(
    descripcion: str = "",
    color: str = "",
    categoria: str = "",
    solo_en_inventario: bool = False,
    limite: int = 20,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    candidatos = catalog.search_elements(
        session,
        query=descripcion or None,
        color=color or None,
        category=categoria or None,
        only_in_stock=solo_en_inventario,
        limit=max(1, min(limite, 100)),
    )
    return {"encontradas": len(candidatos), "piezas": catalog.candidates_to_dicts(session, candidatos)}


@router.get("/movimientos")
def movimientos(
    element_id: str = "", limite: int = 50, session: Session = Depends(get_session)
) -> dict[str, Any]:
    return {"movimientos": inventory.movements(session, element_id or None, limite)}


# --------------------------------------------------------------------------
# Sets
# --------------------------------------------------------------------------
@router.get("/sets")
def listar_sets(session: Session = Depends(get_session)) -> dict[str, Any]:
    salida = []
    for lego_set in session.scalars(select(LegoSet).order_by(LegoSet.set_number)):
        piezas = (
            session.scalar(
                select(func.sum(SetPart.quantity)).where(SetPart.set_number == lego_set.set_number)
            )
            or 0
        )
        referencias = (
            session.scalar(
                select(func.count(SetPart.id)).where(SetPart.set_number == lego_set.set_number)
            )
            or 0
        )
        salida.append(
            {
                "set": lego_set.set_number,
                "nombre": lego_set.name,
                "piezas": int(piezas),
                "referencias": int(referencias),
                "inventariado": lego_set.owned,
            }
        )
    return {"total": len(salida), "sets": salida}


@router.get("/sets/{numero_set}")
def detalle_set(numero_set: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    numero = numero_set if "-" in numero_set else f"{numero_set}-1"
    lego_set = session.get(LegoSet, numero)
    if lego_set is None:
        raise HTTPException(status_code=404, detail={"error": f"El set {numero} no existe."})

    lineas = list(session.scalars(select(SetPart).where(SetPart.set_number == numero)))
    disponibilidad = inventory.availability(session, [l.element_id for l in lineas])
    piezas = []
    for linea in lineas:
        element = session.get(Element, linea.element_id)
        piezas.append(
            {
                "element_id": linea.element_id,
                "pieza": element.part.name if element else linea.element_id,
                "color": element.color.name if element else None,
                "imagen": catalog.absolute_image_url(element.image_url) if element else None,
                "cantidad_en_set": linea.quantity,
                **disponibilidad.get(linea.element_id, {}),
            }
        )
    piezas.sort(key=lambda p: -p["cantidad_en_set"])
    return {
        "set": numero,
        "nombre": lego_set.name,
        "inventariado": lego_set.owned,
        "referencias": len(lineas),
        "piezas_totales": sum(l.quantity for l in lineas),
        "piezas": piezas,
    }


@router.post("/sets/importar")
def importar_csv(datos: ImportacionCSV, session: Session = Depends(get_session)) -> dict[str, Any]:
    resultado = _ejecutar(
        importers.import_set_csv,
        session,
        datos.contenido_csv,
        set_number=datos.numero_set,
        set_name=datos.nombre_set,
    )
    session.commit()
    return resultado.to_dict()


@router.post("/sets/importar-fichero")
async def importar_fichero(
    fichero: UploadFile = File(...),
    numero_set: str = "",
    nombre_set: str = "",
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    crudo = await fichero.read()
    try:
        contenido = crudo.decode("utf-8-sig")
    except UnicodeDecodeError:
        contenido = crudo.decode("cp1252", errors="replace")
    resultado = _ejecutar(
        importers.import_set_csv,
        session,
        contenido,
        set_number=numero_set or None,
        set_name=nombre_set or None,
    )
    session.commit()
    return resultado.to_dict()


@router.post("/sets/{numero_set}/inventariar")
def inventariar_set(
    numero_set: str, datos: InventariarSet, session: Session = Depends(get_session)
) -> dict[str, Any]:
    resultado = _ejecutar(
        inventory.add_set_to_inventory,
        session,
        numero_set,
        datos.veces,
        datos.ubicacion,
        confirmar=datos.confirmar,
    )
    session.commit()
    return resultado


# --------------------------------------------------------------------------
# Montajes
# --------------------------------------------------------------------------
@router.get("/montajes")
def listar_montajes(estado: str = "", session: Session = Depends(get_session)) -> dict[str, Any]:
    return {"montajes": builds.list_builds(session, estado or None)}


@router.post("/montajes")
def crear_montaje(datos: NuevoMontaje, session: Session = Depends(get_session)) -> dict[str, Any]:
    resultado = _ejecutar(builds.create_build, session, datos.nombre, datos.descripcion)
    session.commit()
    return resultado


@router.get("/montajes/{montaje_id}")
def ver_montaje(montaje_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    return _ejecutar(builds.build_detail, session, montaje_id)


@router.delete("/montajes/{montaje_id}")
def borrar_montaje(montaje_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    resultado = _ejecutar(builds.delete_build, session, montaje_id)
    session.commit()
    return resultado


@router.post("/montajes/{montaje_id}/pasos")
def anadir_paso(
    montaje_id: int, datos: NuevoPaso, session: Session = Depends(get_session)
) -> dict[str, Any]:
    resultado = _ejecutar(
        builds.add_step,
        session,
        montaje_id,
        datos.titulo,
        parts=datos.piezas,
        instruction=datos.instruccion,
        position=datos.posicion,
        permitir_faltantes=datos.permitir_faltantes,
    )
    session.commit()
    return resultado


@router.post("/montajes/{montaje_id}/estado")
def estado_montaje(
    montaje_id: int, datos: CambioEstado, session: Session = Depends(get_session)
) -> dict[str, Any]:
    resultado = _ejecutar(builds.set_build_status, session, montaje_id, datos.estado)
    session.commit()
    return resultado


@router.get("/montajes/{montaje_id}/comprobar")
def comprobar_montaje(montaje_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    return _ejecutar(builds.check_build, session, montaje_id)


@router.post("/pasos/{paso_id}/estado")
def estado_paso(
    paso_id: int, datos: CambioEstado, session: Session = Depends(get_session)
) -> dict[str, Any]:
    resultado = _ejecutar(builds.set_step_status, session, paso_id, datos.estado)
    session.commit()
    return resultado


@router.patch("/pasos/{paso_id}")
def editar_paso(
    paso_id: int, datos: EdicionPaso, session: Session = Depends(get_session)
) -> dict[str, Any]:
    resultado = _ejecutar(
        builds.update_step,
        session,
        paso_id,
        title=datos.titulo,
        instruction=datos.instruccion,
        parts=datos.piezas,
        permitir_faltantes=datos.permitir_faltantes,
    )
    session.commit()
    return resultado


@router.delete("/pasos/{paso_id}")
def borrar_paso(paso_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    resultado = _ejecutar(builds.remove_step, session, paso_id)
    session.commit()
    return resultado


@router.get("/que-puedo-montar")
def que_puedo_montar(
    cobertura_minima: float = 0.0, session: Session = Depends(get_session)
) -> dict[str, Any]:
    sets = builds.buildable_sets(session, umbral=max(0.0, min(cobertura_minima, 1.0)))
    return {
        "completos": [s for s in sets if s["completo"]],
        "parciales": [s for s in sets if not s["completo"]],
    }


# --------------------------------------------------------------------------
# Diseñador 3D
# --------------------------------------------------------------------------
@router.get("/piezas/mallas")
def mallas_de_piezas(
    tareas: BackgroundTasks, design_ids: str = "", session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Geometría real de los moldes pedidos (los que ya estén convertidos).

    Lo que todavía no esté en la caché se prepara en segundo plano y se avisa en
    `generando`: la petición no se queda esperando a la biblioteca LDraw, y
    mientras tanto el diseñador dibuja la caja aproximada.
    """
    pedidos = [d.strip() for d in design_ids.split(",") if d.strip()][:120]
    listas: dict[str, Any] = {}
    generando: list[str] = []
    for design_id in pedidos:
        if ldraw.hay_malla(design_id):
            datos = ldraw.malla_o_nada(design_id, permitir_descarga=False)
            if datos:
                listas[design_id] = datos
                continue
        generando.append(design_id)
        tareas.add_task(ldraw.malla_o_nada, design_id)
    return {"mallas": listas, "generando": generando}


@router.get("/montajes/{montaje_id}/modelo")
def modelo_3d(
    montaje_id: int, mapa: bool = False, session: Session = Depends(get_session)
) -> dict[str, Any]:
    return _ejecutar(designer.modelo, session, montaje_id, con_mapa=mapa)


@router.get("/montajes/{montaje_id}/instrucciones")
def instrucciones(montaje_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    return _ejecutar(designer.instrucciones, session, montaje_id)


@router.get("/montajes/{montaje_id}/paleta")
def paleta(
    montaje_id: int, limite: int = 200, session: Session = Depends(get_session)
) -> dict[str, Any]:
    return _ejecutar(designer.piezas_utilizables, session, montaje_id, limite)


@router.post("/montajes/{montaje_id}/piezas")
def colocar_piezas(
    montaje_id: int, datos: ColocacionPiezas, session: Session = Depends(get_session)
) -> dict[str, Any]:
    resultado = _ejecutar(
        designer.colocar_piezas,
        session,
        montaje_id,
        datos.piezas,
        paso_id=datos.paso_id,
        titulo_paso=datos.titulo_paso,
        permitir_faltantes=datos.permitir_faltantes,
    )
    session.commit()
    return resultado


@router.patch("/colocaciones/{colocacion_id}")
def mover_pieza(
    colocacion_id: int, datos: MovimientoPieza, session: Session = Depends(get_session)
) -> dict[str, Any]:
    resultado = _ejecutar(
        designer.mover_pieza,
        session,
        colocacion_id,
        x=datos.x,
        z=datos.z,
        y=datos.y,
        rotacion=datos.rotacion,
    )
    session.commit()
    return resultado


@router.delete("/colocaciones/{colocacion_id}")
def quitar_pieza(colocacion_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    resultado = _ejecutar(designer.quitar_pieza, session, colocacion_id)
    session.commit()
    return resultado


@router.post("/montajes/{montaje_id}/vaciar")
def vaciar_modelo(
    montaje_id: int, paso_id: int | None = None, session: Session = Depends(get_session)
) -> dict[str, Any]:
    resultado = _ejecutar(designer.vaciar, session, montaje_id, paso_id)
    session.commit()
    return resultado


@router.post("/montajes/{montaje_id}/placa")
def cambiar_placa(
    montaje_id: int, datos: TamanoPlaca, session: Session = Depends(get_session)
) -> dict[str, Any]:
    resultado = _ejecutar(designer.configurar_placa, session, montaje_id, datos.ancho, datos.fondo)
    session.commit()
    return resultado


# --------------------------------------------------------------------------
# Editor: operaciones sobre una selección de piezas
# --------------------------------------------------------------------------
@router.post("/montajes/{montaje_id}/edicion/mover")
def edicion_mover(
    montaje_id: int, datos: MovimientoLote, session: Session = Depends(get_session)
) -> dict[str, Any]:
    resultado = _ejecutar(
        editor.mover,
        session,
        montaje_id,
        datos.ids,
        dx=datos.dx,
        dy=datos.dy,
        dz=datos.dz,
        apoyar=datos.apoyar,
    )
    session.commit()
    return resultado


@router.post("/montajes/{montaje_id}/edicion/girar")
def edicion_girar(
    montaje_id: int, datos: GiroLote, session: Session = Depends(get_session)
) -> dict[str, Any]:
    resultado = _ejecutar(editor.girar, session, montaje_id, datos.ids, grados=datos.grados)
    session.commit()
    return resultado


@router.post("/montajes/{montaje_id}/edicion/reflejar")
def edicion_reflejar(
    montaje_id: int, datos: EspejoLote, session: Session = Depends(get_session)
) -> dict[str, Any]:
    resultado = _ejecutar(editor.reflejar, session, montaje_id, datos.ids, eje=datos.eje)
    session.commit()
    return resultado


@router.post("/montajes/{montaje_id}/edicion/duplicar")
def edicion_duplicar(
    montaje_id: int, datos: DuplicadoLote, session: Session = Depends(get_session)
) -> dict[str, Any]:
    resultado = _ejecutar(
        editor.duplicar,
        session,
        montaje_id,
        datos.ids,
        dx=datos.dx,
        dy=datos.dy,
        dz=datos.dz,
        paso_id=datos.paso_id,
        titulo_paso=datos.titulo_paso,
        permitir_faltantes=datos.permitir_faltantes,
    )
    session.commit()
    return resultado


@router.post("/montajes/{montaje_id}/edicion/sustituir")
def edicion_sustituir(
    montaje_id: int, datos: SustitucionLote, session: Session = Depends(get_session)
) -> dict[str, Any]:
    resultado = _ejecutar(
        editor.sustituir,
        session,
        montaje_id,
        datos.ids,
        element_id=datos.element_id,
        color=datos.color,
        descripcion=datos.descripcion,
        permitir_faltantes=datos.permitir_faltantes,
    )
    session.commit()
    return resultado


@router.post("/montajes/{montaje_id}/edicion/paso")
def edicion_paso(
    montaje_id: int, datos: PasoDeLote, session: Session = Depends(get_session)
) -> dict[str, Any]:
    resultado = _ejecutar(
        editor.cambiar_de_paso,
        session,
        montaje_id,
        datos.ids,
        paso_id=datos.paso_id,
        titulo_paso=datos.titulo_paso,
    )
    session.commit()
    return resultado


@router.post("/montajes/{montaje_id}/edicion/quitar")
def edicion_quitar(
    montaje_id: int, datos: Seleccion, session: Session = Depends(get_session)
) -> dict[str, Any]:
    resultado = _ejecutar(editor.quitar, session, montaje_id, datos.ids)
    session.commit()
    return resultado


@router.get("/montajes/{montaje_id}/seleccion")
def edicion_seleccionar(
    montaje_id: int,
    element_id: str = "",
    design_id: str = "",
    color: str = "",
    pieza: str = "",
    paso_id: int = 0,
    y: int | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    return _ejecutar(
        editor.seleccionar,
        session,
        montaje_id,
        element_id=element_id or None,
        design_id=design_id or None,
        color=color or None,
        pieza=pieza or None,
        paso_id=paso_id or None,
        y=y,
    )


@router.get("/elementos/{element_id}/colores")
def colores_de_pieza(
    element_id: str, todos: bool = False, session: Session = Depends(get_session)
) -> dict[str, Any]:
    return _ejecutar(editor.colores_de_pieza, session, element_id, solo_disponibles=not todos)


# --------------------------------------------------------------------------
# Deshacer y rehacer
# --------------------------------------------------------------------------
@router.post("/montajes/{montaje_id}/deshacer")
def deshacer(montaje_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    resultado = _ejecutar(historial.deshacer, session, montaje_id)
    session.commit()
    return resultado


@router.post("/montajes/{montaje_id}/rehacer")
def rehacer(montaje_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    resultado = _ejecutar(historial.rehacer, session, montaje_id)
    session.commit()
    return resultado


@router.get("/montajes/{montaje_id}/historial")
def ver_historial(
    montaje_id: int, limite: int = 20, session: Session = Depends(get_session)
) -> dict[str, Any]:
    return _ejecutar(historial.lista, session, montaje_id, limite)


# --------------------------------------------------------------------------
# Reconocimiento
# --------------------------------------------------------------------------
@router.get("/reconocimientos")
def listar_reconocimientos(
    estado: str = "", limite: int = 20, session: Session = Depends(get_session)
) -> dict[str, Any]:
    return {"sesiones": recognition.listar_sesiones(session, estado or None, limite)}


@router.post("/reconocimientos")
def crear_reconocimiento(
    datos: DeteccionesImagen, session: Session = Depends(get_session)
) -> dict[str, Any]:
    resultado = _ejecutar(
        recognition.crear_sesion,
        session,
        datos.detecciones,
        imagen_base64=datos.imagen_base64,
        notas=datos.notas,
    )
    session.commit()
    return resultado


@router.get("/reconocimientos/{sesion_id}")
def ver_reconocimiento(sesion_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    return _ejecutar(recognition.detalle_sesion, session, sesion_id)


@router.post("/reconocimientos/{sesion_id}/resolver")
def resolver_reconocimiento(
    sesion_id: int, datos: ResolucionesReconocimiento, session: Session = Depends(get_session)
) -> dict[str, Any]:
    resultado = _ejecutar(recognition.resolver_items, session, sesion_id, datos.resoluciones)
    session.commit()
    return resultado


@router.post("/reconocimientos/{sesion_id}/confirmar")
def confirmar_reconocimiento(
    sesion_id: int, datos: ConfirmacionReconocimiento, session: Session = Depends(get_session)
) -> dict[str, Any]:
    resultado = _ejecutar(
        recognition.confirmar_sesion,
        session,
        sesion_id,
        ubicacion=datos.ubicacion,
        solo_resueltas=datos.ignorar_pendientes,
    )
    session.commit()
    return resultado


@router.post("/reconocimientos/{sesion_id}/descartar")
def descartar_reconocimiento(sesion_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    resultado = _ejecutar(recognition.descartar_sesion, session, sesion_id)
    session.commit()
    return resultado
