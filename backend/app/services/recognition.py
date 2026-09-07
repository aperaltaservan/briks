"""Inventariado a partir de una foto.

Cómo funciona el reparto de responsabilidades:

  1. El modelo del chat (que sí ve la imagen) describe lo que hay:
     "4 placas 2x4 rojas, 2 ladrillos 1x2 beige...".
  2. Esas descripciones entran aquí y se resuelven contra el catálogo real.
  3. Lo que se resuelve con confianza queda listo; lo dudoso se deja pendiente
     con candidatos, para preguntar antes de tocar el inventario.
  4. Sólo al confirmar se suman las piezas.

Este servidor no ejecuta visión artificial: el reconocimiento visual lo aporta
el modelo multimodal del chat. Aquí está la parte que él no puede hacer bien,
que es casar una descripción con el ElementID correcto y llevar la contabilidad.
"""
from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Element, RecognitionItem, RecognitionSession
from . import catalog, inventory

# Umbral por debajo del cual preferimos preguntar antes que inventariar.
UMBRAL_AUTOMATICO = 6.0

_FIRMAS_IMAGEN = {
    b"\xff\xd8\xff": "jpg",
    b"\x89PNG\r\n\x1a\n": "png",
    b"GIF87a": "gif",
    b"GIF89a": "gif",
    b"RIFF": "webp",
}


class RecognitionError(Exception):
    """Error de negocio del flujo de reconocimiento."""


def guardar_imagen(datos_base64: str) -> str:
    """Guarda la foto recibida y devuelve su ruta relativa.

    Sirve como prueba de lo que se inventarió: útil para revisar después.
    """
    limpio = datos_base64.strip()
    if limpio.startswith("data:"):
        _, _, limpio = limpio.partition(",")
    try:
        binario = base64.b64decode(limpio, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RecognitionError(f"La imagen no es base64 válido: {exc}") from exc
    if not binario:
        raise RecognitionError("La imagen está vacía.")

    extension = next(
        (ext for firma, ext in _FIRMAS_IMAGEN.items() if binario.startswith(firma)), "bin"
    )
    marca = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    nombre = f"{marca}-{uuid.uuid4().hex[:8]}.{extension}"
    destino = settings.uploads_dir / nombre
    destino.write_bytes(binario)
    return nombre


# --------------------------------------------------------------------------
# Creación de la sesión de reconocimiento
# --------------------------------------------------------------------------
def crear_sesion(
    session: Session,
    detecciones: Iterable[dict[str, Any]],
    imagen_base64: str | None = None,
    notas: str | None = None,
    origen: str = "imagen",
) -> dict[str, Any]:
    """Registra un lote de piezas detectadas y las intenta resolver.

    Cada detección: {descripcion, color, cantidad, element_id?, design_id?}
    """
    lista = list(detecciones or [])
    if not lista:
        raise RecognitionError("No se ha recibido ninguna pieza detectada.")

    imagen_ref = guardar_imagen(imagen_base64) if imagen_base64 else None

    rec = RecognitionSession(source=origen, image_ref=imagen_ref, notes=notas)
    session.add(rec)
    session.flush()

    for deteccion in lista:
        _crear_item(session, rec, deteccion)

    session.flush()
    return detalle_sesion(session, rec.id)


def _crear_item(session: Session, rec: RecognitionSession, deteccion: dict[str, Any]) -> RecognitionItem:
    descripcion = str(deteccion.get("descripcion") or deteccion.get("pieza") or "").strip()
    color = (str(deteccion.get("color")).strip() if deteccion.get("color") else None)
    try:
        cantidad = int(deteccion.get("cantidad") or deteccion.get("quantity") or 1)
    except (TypeError, ValueError):
        cantidad = 1
    cantidad = max(1, cantidad)

    item = RecognitionItem(
        session_id=rec.id,
        raw_description=descripcion or "(sin descripción)",
        raw_color=color,
        quantity=cantidad,
    )

    element_id = deteccion.get("element_id")
    if element_id and session.get(Element, str(element_id).strip()):
        item.element_id = str(element_id).strip()
        item.status = "resuelto"
        item.confidence = 1.0
        session.add(item)
        return item

    candidatos = catalog.search_elements(
        session,
        query=descripcion,
        color=color,
        design_id=deteccion.get("design_id"),
        limit=6,
    )
    # Si el color deja la búsqueda vacía (p. ej. no tengo ninguna pieza verde),
    # buscamos por forma para poder ofrecer alternativas en vez de nada.
    if not candidatos and color:
        candidatos = catalog.search_elements(
            session, query=descripcion, design_id=deteccion.get("design_id"), limit=6
        )
        # Sin confirmar el color, nunca resolvemos solos.
        if candidatos:
            item.candidates = json.dumps(
                catalog.candidates_to_dicts(session, candidatos), ensure_ascii=False
            )
            item.status = "pendiente"
            item.confidence = 0.0
            session.add(item)
            return item

    if candidatos:
        item.candidates = json.dumps(
            catalog.candidates_to_dicts(session, candidatos), ensure_ascii=False
        )
        mejor = candidatos[0]
        segundo = candidatos[1].score if len(candidatos) > 1 else 0.0
        # Resolvemos solo si el mejor candidato destaca claramente sobre el resto.
        if mejor.score >= UMBRAL_AUTOMATICO and mejor.score > segundo * 1.2:
            item.element_id = mejor.element.element_id
            item.status = "resuelto"
            item.confidence = round(min(1.0, mejor.score / 15.0), 2)
        else:
            item.status = "pendiente"
            item.confidence = round(min(1.0, mejor.score / 15.0), 2)
    else:
        item.status = "pendiente"
        item.confidence = 0.0

    session.add(item)
    return item


# --------------------------------------------------------------------------
# Consulta, resolución y confirmación
# --------------------------------------------------------------------------
def _require_sesion(session: Session, session_id: int) -> RecognitionSession:
    rec = session.get(RecognitionSession, int(session_id))
    if rec is None:
        raise RecognitionError(f"No existe la sesión de reconocimiento {session_id}.")
    return rec


def detalle_sesion(session: Session, session_id: int) -> dict[str, Any]:
    rec = _require_sesion(session, session_id)
    items = list(session.scalars(select(RecognitionItem).where(RecognitionItem.session_id == rec.id)))

    resueltos, pendientes, descartados = [], [], []
    for item in items:
        datos: dict[str, Any] = {
            "id": item.id,
            "detectado": item.raw_description,
            "color_detectado": item.raw_color,
            "cantidad": item.quantity,
            "estado": item.status,
            "confianza": item.confidence,
        }
        if item.element_id:
            element = session.get(Element, item.element_id)
            if element:
                datos["element_id"] = element.element_id
                datos["pieza"] = element.part.name
                datos["color"] = element.color.name
                datos["imagen"] = catalog.absolute_image_url(element.image_url)
        if item.status == "pendiente" and item.candidates:
            datos["candidatos"] = json.loads(item.candidates)

        if item.status == "resuelto":
            resueltos.append(datos)
        elif item.status == "descartado":
            descartados.append(datos)
        else:
            pendientes.append(datos)

    return {
        "sesion_id": rec.id,
        "estado": rec.status,
        "origen": rec.source,
        "imagen": f"/media/{rec.image_ref}" if rec.image_ref else None,
        "notas": rec.notes,
        "creada": rec.created_at.isoformat(),
        "resumen": {
            "resueltas": len(resueltos),
            "pendientes": len(pendientes),
            "descartadas": len(descartados),
            "piezas_a_inventariar": sum(i["cantidad"] for i in resueltos),
        },
        "resueltas": resueltos,
        "pendientes": pendientes,
        "descartadas": descartados,
    }


def resolver_items(
    session: Session, session_id: int, resoluciones: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    """Fija manualmente el elemento (o descarta) de las detecciones dudosas.

    Cada resolución: {item_id, element_id} o {item_id, descartar: true},
    opcionalmente con "cantidad" para corregir el recuento.
    """
    rec = _require_sesion(session, session_id)
    if rec.status != "pendiente":
        raise RecognitionError(f"La sesión {rec.id} ya está {rec.status}; no admite cambios.")

    aplicadas = 0
    for resolucion in resoluciones or []:
        item_id = resolucion.get("item_id")
        item = session.get(RecognitionItem, int(item_id)) if item_id else None
        if item is None or item.session_id != rec.id:
            raise RecognitionError(f"La línea {item_id!r} no pertenece a la sesión {rec.id}.")

        if "cantidad" in resolucion and resolucion["cantidad"] is not None:
            item.quantity = max(1, int(resolucion["cantidad"]))

        if resolucion.get("descartar"):
            item.status = "descartado"
            item.element_id = None
            aplicadas += 1
            continue

        element_id = str(resolucion.get("element_id") or "").strip()
        if not element_id:
            raise RecognitionError(
                f"La línea {item.id} necesita un element_id o descartar: true."
            )
        if session.get(Element, element_id) is None:
            raise RecognitionError(f"El elemento {element_id!r} no existe en el catálogo.")

        item.element_id = element_id
        item.status = "resuelto"
        item.confidence = 1.0
        aplicadas += 1

    session.flush()
    detalle = detalle_sesion(session, rec.id)
    detalle["lineas_actualizadas"] = aplicadas
    return detalle


def confirmar_sesion(
    session: Session, session_id: int, ubicacion: str = "", solo_resueltas: bool = True
) -> dict[str, Any]:
    """Aplica al inventario las detecciones resueltas. Es el único paso que suma piezas."""
    rec = _require_sesion(session, session_id)
    if rec.status == "aplicada":
        raise RecognitionError(f"La sesión {rec.id} ya se aplicó al inventario.")
    if rec.status == "descartada":
        raise RecognitionError(f"La sesión {rec.id} está descartada.")

    items = list(session.scalars(select(RecognitionItem).where(RecognitionItem.session_id == rec.id)))
    pendientes = [i for i in items if i.status == "pendiente"]
    if pendientes and not solo_resueltas:
        raise RecognitionError(
            f"Quedan {len(pendientes)} líneas sin resolver. Resuélvelas o descártalas primero."
        )

    aplicadas = []
    for item in items:
        if item.status != "resuelto" or not item.element_id:
            continue
        inventory.add_stock(
            session,
            item.element_id,
            item.quantity,
            location=ubicacion,
            reason="imagen",
            reference=f"reconocimiento#{rec.id}",
        )
        element = session.get(Element, item.element_id)
        aplicadas.append(
            {
                "element_id": item.element_id,
                "pieza": element.part.name if element else item.element_id,
                "color": element.color.name if element else None,
                "cantidad": item.quantity,
            }
        )

    rec.status = "aplicada"
    session.flush()

    return {
        "sesion_id": rec.id,
        "estado": rec.status,
        "ubicacion": ubicacion or None,
        "lineas_aplicadas": len(aplicadas),
        "piezas_anadidas": sum(a["cantidad"] for a in aplicadas),
        "sin_resolver": len(pendientes),
        "detalle": aplicadas,
    }


def descartar_sesion(session: Session, session_id: int) -> dict[str, Any]:
    rec = _require_sesion(session, session_id)
    if rec.status == "aplicada":
        raise RecognitionError("No se puede descartar una sesión ya aplicada al inventario.")
    rec.status = "descartada"
    session.flush()
    return {"sesion_id": rec.id, "estado": rec.status}


def listar_sesiones(session: Session, estado: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    stmt = select(RecognitionSession).order_by(RecognitionSession.created_at.desc()).limit(limit)
    if estado:
        stmt = stmt.where(RecognitionSession.status == estado.strip().lower())
    salida = []
    for rec in session.scalars(stmt):
        items = list(
            session.scalars(select(RecognitionItem).where(RecognitionItem.session_id == rec.id))
        )
        salida.append(
            {
                "sesion_id": rec.id,
                "estado": rec.status,
                "origen": rec.source,
                "imagen": f"/media/{rec.image_ref}" if rec.image_ref else None,
                "lineas": len(items),
                "pendientes": sum(1 for i in items if i.status == "pendiente"),
                "creada": rec.created_at.isoformat(),
            }
        )
    return salida
