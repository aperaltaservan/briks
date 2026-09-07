"""Inventario: existencias, movimientos y disponibilidad real.

Distinción importante:
  - existencias  -> lo que tengo físicamente.
  - reservado    -> lo que está usado por montajes activos.
  - disponible   -> existencias - reservado. Es lo que puedo usar en un montaje nuevo.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from ..models import (
    BUILD_ACTIVE_STATES,
    Build,
    BuildStep,
    BuildStepPart,
    Color,
    Element,
    InventoryItem,
    InventoryMovement,
    LegoSet,
    Part,
    SetPart,
)
from . import catalog


class InventoryError(Exception):
    """Error de negocio del inventario (elemento inexistente, stock insuficiente...)."""


class SetYaInventariadoError(InventoryError):
    """El set ya se dio de alta antes. Repetirlo duplicaría las piezas.

    No es un fallo, es una pregunta: quien llama decide si tiene una segunda
    copia del set (entonces confirma) o si se estaba equivocando.
    """

    requiere_confirmacion = True

    def __init__(self, mensaje: str, detalle: dict[str, Any]) -> None:
        super().__init__(mensaje)
        self.detalle = detalle


# --------------------------------------------------------------------------
# Reservas y disponibilidad
# --------------------------------------------------------------------------
def reserved_quantities(
    session: Session, element_ids: list[str] | None = None, excluir_build: int | None = None
) -> dict[str, int]:
    """Piezas comprometidas por montajes activos."""
    stmt = (
        select(BuildStepPart.element_id, func.sum(BuildStepPart.quantity))
        .join(BuildStep, BuildStep.id == BuildStepPart.step_id)
        .join(Build, Build.id == BuildStep.build_id)
        .where(Build.status.in_(BUILD_ACTIVE_STATES))
        .group_by(BuildStepPart.element_id)
    )
    if element_ids:
        stmt = stmt.where(BuildStepPart.element_id.in_(element_ids))
    if excluir_build is not None:
        stmt = stmt.where(Build.id != excluir_build)
    return {eid: int(total or 0) for eid, total in session.execute(stmt).all()}


def availability(
    session: Session, element_ids: list[str], excluir_build: int | None = None
) -> dict[str, dict[str, int]]:
    """Existencias, reservado y disponible por elemento."""
    stock = catalog.stock_by_element(session, element_ids)
    reservado = reserved_quantities(session, element_ids, excluir_build=excluir_build)
    resultado: dict[str, dict[str, int]] = {}
    for element_id in element_ids:
        total = stock.get(element_id, 0)
        usado = reservado.get(element_id, 0)
        resultado[element_id] = {
            "existencias": total,
            "reservado": usado,
            "disponible": max(0, total - usado),
        }
    return resultado


# --------------------------------------------------------------------------
# Altas y ajustes
# --------------------------------------------------------------------------
def _require_element(session: Session, element_id: str) -> Element:
    element = session.get(Element, str(element_id).strip())
    if element is None:
        raise InventoryError(
            f"El elemento {element_id!r} no está en el catálogo. "
            "Impórtalo primero desde un set o dalo de alta con alta_pieza_manual."
        )
    return element


def add_stock(
    session: Session,
    element_id: str,
    quantity: int,
    location: str = "",
    reason: str = "manual",
    reference: str | None = None,
) -> dict[str, Any]:
    """Suma (o resta, si quantity es negativa) unidades de un elemento."""
    element = _require_element(session, element_id)
    if quantity == 0:
        raise InventoryError("La cantidad no puede ser cero.")

    ubicacion = (location or "").strip()
    item = session.scalar(
        select(InventoryItem).where(
            InventoryItem.element_id == element.element_id,
            InventoryItem.location == ubicacion,
        )
    )
    if item is None:
        if quantity < 0:
            raise InventoryError(
                f"No hay existencias de {element.element_id} en la ubicación {ubicacion or '(sin ubicación)'}."
            )
        item = InventoryItem(element_id=element.element_id, quantity=0, location=ubicacion)
        session.add(item)
        session.flush()

    nueva = item.quantity + quantity
    if nueva < 0:
        raise InventoryError(
            f"No puedes retirar {abs(quantity)} unidades de {element.element_id}: "
            f"sólo hay {item.quantity}."
        )

    item.quantity = nueva
    session.add(
        InventoryMovement(
            element_id=element.element_id,
            delta=quantity,
            reason=reason,
            reference=reference,
            location=ubicacion,
        )
    )
    session.flush()
    return _item_to_dict(session, item)


def set_stock(
    session: Session,
    element_id: str,
    quantity: int,
    location: str = "",
    reason: str = "ajuste",
    reference: str | None = None,
) -> dict[str, Any]:
    """Fija las existencias a un valor absoluto (recuento manual)."""
    element = _require_element(session, element_id)
    if quantity < 0:
        raise InventoryError("La cantidad no puede ser negativa.")

    ubicacion = (location or "").strip()
    item = session.scalar(
        select(InventoryItem).where(
            InventoryItem.element_id == element.element_id,
            InventoryItem.location == ubicacion,
        )
    )
    anterior = item.quantity if item else 0
    if item is None:
        item = InventoryItem(element_id=element.element_id, quantity=0, location=ubicacion)
        session.add(item)
        session.flush()

    delta = quantity - anterior
    item.quantity = quantity
    if delta:
        session.add(
            InventoryMovement(
                element_id=element.element_id,
                delta=delta,
                reason=reason,
                reference=reference,
                location=ubicacion,
            )
        )
    session.flush()
    return _item_to_dict(session, item)


def historial_de_set(session: Session, set_number: str) -> dict[str, Any]:
    """Qué se ha dado de alta ya de un set concreto."""
    fila = session.execute(
        select(
            func.count(InventoryMovement.id),
            func.sum(InventoryMovement.delta),
            func.max(InventoryMovement.created_at),
        ).where(
            InventoryMovement.reason == "set",
            InventoryMovement.reference == set_number,
        )
    ).one()
    lineas, piezas, ultima = fila
    return {
        "lineas": int(lineas or 0),
        "piezas_anadidas": int(piezas or 0),
        "ultima_vez": ultima.isoformat() if ultima else None,
    }


def add_set_to_inventory(
    session: Session,
    set_number: str,
    veces: int = 1,
    location: str = "",
    incluir_repuestos: bool = True,
    confirmar: bool = False,
) -> dict[str, Any]:
    """Da de alta en inventario todas las piezas de un set ya importado.

    Si el set ya estaba inventariado no se repite sin más: hacerlo duplicaría
    las existencias en silencio. Hay que insistir con `confirmar=True`, que es
    lo correcto cuando de verdad se tienen dos copias del mismo set.
    """
    numero = str(set_number).strip()
    if "-" not in numero:
        numero = f"{numero}-1"
    lego_set = session.get(LegoSet, numero)
    if lego_set is None:
        raise InventoryError(
            f"El set {numero} no está importado. Usa importar_set_csv antes de inventariarlo."
        )
    if veces < 1:
        raise InventoryError("El número de veces debe ser al menos 1.")

    if lego_set.owned and not confirmar:
        previo = historial_de_set(session, numero)
        cuando = (
            f" (la última vez el {previo['ultima_vez'][:10]})" if previo["ultima_vez"] else ""
        )
        raise SetYaInventariadoError(
            f"El set {numero} ya está inventariado: se le sumaron "
            f"{previo['piezas_anadidas']} piezas{cuando}. Volver a inventariarlo las "
            "sumaría OTRA VEZ. Confirma sólo si tienes una copia más del set.",
            {
                "set": numero,
                "nombre": lego_set.name,
                "ya_inventariado": True,
                "veces_solicitadas": veces,
                **previo,
            },
        )

    ya_estaba = bool(lego_set.owned)
    stmt = select(SetPart).where(SetPart.set_number == numero)
    if not incluir_repuestos:
        stmt = stmt.where(SetPart.is_spare.is_(False))

    lineas = list(session.scalars(stmt))
    if not lineas:
        raise InventoryError(f"El set {numero} no tiene piezas registradas.")

    total = 0
    for linea in lineas:
        cantidad = linea.quantity * veces
        add_stock(
            session,
            linea.element_id,
            cantidad,
            location=location,
            reason="set",
            reference=numero,
        )
        total += cantidad

    lego_set.owned = True
    session.flush()
    return {
        "set": numero,
        "nombre": lego_set.name,
        "veces": veces,
        "lineas": len(lineas),
        "piezas_anadidas": total,
        "ubicacion": location or None,
        "repetido": ya_estaba,
    }


# --------------------------------------------------------------------------
# Consultas
# --------------------------------------------------------------------------
def _item_to_dict(session: Session, item: InventoryItem) -> dict[str, Any]:
    element = session.get(Element, item.element_id)
    disponibilidad = availability(session, [item.element_id])[item.element_id]
    return {
        "element_id": item.element_id,
        "design_id": element.design_id if element else None,
        "pieza": element.part.name if element else None,
        "color": element.color.name if element else None,
        "categoria": element.part.category if element else None,
        "imagen": catalog.absolute_image_url(element.image_url) if element else None,
        "ubicacion": item.location or None,
        "existencias_ubicacion": item.quantity,
        **disponibilidad,
    }


def list_inventory(
    session: Session,
    query: str | None = None,
    color: str | None = None,
    category: str | None = None,
    location: str | None = None,
    solo_disponibles: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Lista el inventario agrupado por elemento, con filtros en lenguaje natural."""
    # Partimos de los elementos que coinciden con la búsqueda...
    if query or color or category:
        candidatos = catalog.search_elements(
            session, query=query, color=color, category=category, only_in_stock=True, limit=1000
        )
        ids_permitidos = [c.element.element_id for c in candidatos]
        if not ids_permitidos:
            return {"total": 0, "mostrados": 0, "items": []}
    else:
        ids_permitidos = None

    stmt = (
        select(
            InventoryItem.element_id,
            func.sum(InventoryItem.quantity).label("total"),
            func.group_concat(InventoryItem.location).label("ubicaciones"),
        )
        .group_by(InventoryItem.element_id)
        .having(func.sum(InventoryItem.quantity) > 0)
    )
    if ids_permitidos is not None:
        stmt = stmt.where(InventoryItem.element_id.in_(ids_permitidos))
    if location:
        stmt = stmt.where(InventoryItem.location == location.strip())

    filas = session.execute(stmt).all()
    ids = [f.element_id for f in filas]
    disponibilidad = availability(session, ids)

    elementos = {
        e.element_id: e
        for e in session.scalars(
            select(Element)
            .options(joinedload(Element.part), joinedload(Element.color))
            .where(Element.element_id.in_(ids))
        ).unique()
    }

    items: list[dict[str, Any]] = []
    for fila in filas:
        element = elementos.get(fila.element_id)
        if element is None:
            continue
        disp = disponibilidad[fila.element_id]
        if solo_disponibles and disp["disponible"] <= 0:
            continue
        ubicaciones = sorted({u for u in (fila.ubicaciones or "").split(",") if u})
        items.append(
            {
                "element_id": fila.element_id,
                "design_id": element.design_id,
                "pieza": element.part.name,
                "categoria": element.part.category,
                "color": element.color.name,
                "color_hex": element.color.hex_code,
                "imagen": catalog.absolute_image_url(element.image_url),
                "ubicaciones": ubicaciones or None,
                **disp,
            }
        )

    items.sort(key=lambda i: (-i["existencias"], i["pieza"]))
    total = len(items)
    return {
        "total": total,
        "mostrados": len(items[offset : offset + limit]),
        "items": items[offset : offset + limit],
    }


def inventory_summary(session: Session) -> dict[str, Any]:
    """Cifras globales del inventario, para el panel y para orientar al chat."""
    total_piezas = session.scalar(select(func.sum(InventoryItem.quantity))) or 0
    referencias = (
        session.scalar(
            select(func.count(func.distinct(InventoryItem.element_id))).where(
                InventoryItem.quantity > 0
            )
        )
        or 0
    )

    por_color = session.execute(
        select(Color.name, func.sum(InventoryItem.quantity), Color.hex_code)
        .join(Element, Element.color_id == Color.id)
        .join(InventoryItem, InventoryItem.element_id == Element.element_id)
        .group_by(Color.name, Color.hex_code)
        .order_by(func.sum(InventoryItem.quantity).desc())
    ).all()

    por_categoria = session.execute(
        select(Part.category, func.sum(InventoryItem.quantity))
        .join(Element, Element.design_id == Part.design_id)
        .join(InventoryItem, InventoryItem.element_id == Element.element_id)
        .group_by(Part.category)
        .order_by(func.sum(InventoryItem.quantity).desc())
    ).all()

    reservado = sum(reserved_quantities(session).values())

    return {
        "piezas_totales": int(total_piezas),
        "referencias_distintas": int(referencias),
        "reservadas_en_montajes": reservado,
        "disponibles": max(0, int(total_piezas) - reservado),
        "por_color": [
            {"color": c, "piezas": int(n or 0), "hex": h} for c, n, h in por_color
        ],
        "por_categoria": [
            {"categoria": c or "Sin categoría", "piezas": int(n or 0)} for c, n in por_categoria
        ],
        "catalogo": catalog.catalog_stats(session),
        "sets_importados": session.scalar(select(func.count(LegoSet.set_number))) or 0,
    }


def movements(session: Session, element_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    stmt = select(InventoryMovement).order_by(InventoryMovement.created_at.desc()).limit(limit)
    if element_id:
        stmt = stmt.where(InventoryMovement.element_id == str(element_id).strip())
    return [
        {
            "id": m.id,
            "element_id": m.element_id,
            "delta": m.delta,
            "motivo": m.reason,
            "referencia": m.reference,
            "ubicacion": m.location or None,
            "fecha": m.created_at.isoformat(),
        }
        for m in session.scalars(stmt)
    ]
