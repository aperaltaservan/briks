"""Catálogo: alta de colores/piezas/elementos y búsqueda por lenguaje natural."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from ..config import settings
from ..models import Color, Element, InventoryItem, Part
from . import vocab

# Colores que LEGO nombra como transparentes.
_TRANSPARENT_PREFIXES = ("transparent", "trans")


def absolute_image_url(url: str | None) -> str | None:
    """Las ImageURL de los CSV de LEGO son rutas relativas al CDN."""
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return url
    return f"{settings.lego_cdn_base}{url}"


# --------------------------------------------------------------------------
# Altas idempotentes
# --------------------------------------------------------------------------
def get_or_create_color(session: Session, name: str, hex_code: str | None = None) -> Color:
    nombre = (name or "Desconocido").strip()
    color = session.scalar(select(Color).where(func.lower(Color.name) == nombre.lower()))
    if color:
        if hex_code and not color.hex_code:
            color.hex_code = hex_code
        return color
    color = Color(
        name=nombre,
        hex_code=hex_code or vocab.hex_for_color(nombre),
        is_transparent=vocab.normalize(nombre).startswith(_TRANSPARENT_PREFIXES),
    )
    session.add(color)
    session.flush()
    return color


def build_search_text(name: str, category: str | None, design_id: str) -> str:
    return vocab.normalize(f"{name} {category or ''} {design_id}")


def get_or_create_part(
    session: Session, design_id: str, name: str, category: str | None = None
) -> Part:
    design_id = str(design_id).strip()
    part = session.get(Part, design_id)
    if part:
        # El nombre más largo suele ser el más descriptivo; nos quedamos con él.
        if name and len(name) > len(part.name or ""):
            part.name = name
        if category and not part.category:
            part.category = category
        part.search_text = build_search_text(part.name, part.category, design_id)
        return part
    part = Part(
        design_id=design_id,
        name=name or design_id,
        category=category,
        search_text=build_search_text(name or design_id, category, design_id),
    )
    session.add(part)
    session.flush()
    return part


def get_or_create_element(
    session: Session,
    element_id: str,
    design_id: str,
    color_id: int,
    image_url: str | None = None,
) -> Element:
    element_id = str(element_id).strip()
    element = session.get(Element, element_id)
    if element:
        if image_url and not element.image_url:
            element.image_url = image_url
        return element
    element = Element(
        element_id=element_id,
        design_id=design_id,
        color_id=color_id,
        image_url=image_url,
    )
    session.add(element)
    session.flush()
    return element


# --------------------------------------------------------------------------
# Búsqueda
# --------------------------------------------------------------------------
@dataclass
class Candidate:
    element: Element
    score: float
    reasons: list[str] = field(default_factory=list)

    def to_dict(self, quantity: int = 0, available: int | None = None) -> dict[str, Any]:
        datos: dict[str, Any] = {
            "element_id": self.element.element_id,
            "design_id": self.element.design_id,
            "pieza": self.element.part.name,
            "categoria": self.element.part.category,
            "color": self.element.color.name,
            "color_hex": self.element.color.hex_code,
            "imagen": absolute_image_url(self.element.image_url),
            "en_inventario": quantity,
            "score": round(self.score, 2),
            "coincidencias": self.reasons,
        }
        if available is not None:
            datos["disponibles"] = available
        return datos


_SEPARADORES = re.compile(r"[\s/,.]+")


def _score_part(
    part: Part, formas: list[str], dims: list[str], libres: list[str]
) -> tuple[float, list[str]]:
    """Puntúa una pieza contra la consulta ya traducida a términos LEGO.

    Criterios, por orden de importancia:
      1. La forma ("plate", "brick") debe aparecer en el NOMBRE. Que sólo
         coincida con la categoría es una pista débil: media categoría "Plates"
         no son placas rectas.
      2. La dimensión ("2x4") es lo más discriminante.
      3. Se penalizan las variantes con palabras que nadie pidió, para que
         "PLATE 2X4" gane a "LEFT PLATE 2X4 W/ANGLE" ante la consulta "placa 2x4".
    """
    nombre = vocab.normalize(part.name)
    categoria = vocab.normalize(part.category or "")
    palabras = {p for p in _SEPARADORES.split(nombre) if p}

    score = 0.0
    razones: list[str] = []
    forma_en_nombre = False

    for termino in formas:
        if termino in palabras:
            score += 4.0
            forma_en_nombre = True
            razones.append(termino)
        elif termino in nombre:
            score += 3.0
            forma_en_nombre = True
            razones.append(termino)
        elif termino and termino in categoria:
            score += 0.5
            razones.append(f"categoría:{termino}")

    for dim in dims:
        if dim in palabras:
            score += 6.0
            razones.append(dim)
        elif dim in nombre:
            score += 4.0
            razones.append(dim)

    for token in libres:
        if len(token) < 2 or token in dims:
            continue
        if token in palabras:
            score += 1.5
            razones.append(token)
        elif token in nombre:
            score += 0.8

    # Se pidió una forma concreta y esta pieza no la tiene: casi seguro no es.
    if formas and not forma_en_nombre:
        score *= 0.35

    # Cuanto más "adornado" es el nombre respecto a lo pedido, menos encaja.
    if score > 0:
        pedidas = set(formas) | set(dims) | set(libres)
        extras = sum(1 for p in palabras if p not in pedidas and not p.isdigit())
        score -= min(3.0, 0.5 * extras)

    return max(0.0, score), razones


def search_elements(
    session: Session,
    query: str | None = None,
    color: str | None = None,
    category: str | None = None,
    design_id: str | None = None,
    element_id: str | None = None,
    only_in_stock: bool = False,
    limit: int = 20,
) -> list[Candidate]:
    """Busca elementos combinando descripción libre, color y categoría.

    La consulta puede venir en español ("placa 2x4 roja"); se traduce a la
    nomenclatura LEGO antes de puntuar.
    """
    # Atajo: si dan un ElementID exacto, no hay nada que adivinar.
    if element_id:
        element = session.get(Element, str(element_id).strip())
        return [Candidate(element=element, score=100.0, reasons=["element_id"])] if element else []

    stmt = select(Element).options(joinedload(Element.part), joinedload(Element.color))

    if design_id:
        stmt = stmt.where(Element.design_id == str(design_id).strip())

    categoria = vocab.resolve_category(category) or (category.strip() if category else None)
    if categoria:
        stmt = stmt.join(Part).where(Part.category == categoria)

    # El color puede venir como nombre LEGO exacto o en lenguaje natural.
    colores_preferidos: list[str] = []
    if color:
        colores_preferidos = vocab.resolve_color_names(color)
        exacto = session.scalar(
            select(Color).where(func.lower(Color.name) == color.strip().lower())
        )
        if exacto:
            colores_preferidos = [exacto.name] + [c for c in colores_preferidos if c != exacto.name]
        if colores_preferidos:
            stmt = stmt.join(Color).where(Color.name.in_(colores_preferidos))
        else:
            # Color desconocido: al menos filtramos por coincidencia parcial.
            stmt = stmt.join(Color).where(Color.name.ilike(f"%{color.strip()}%"))

    if only_in_stock:
        stmt = stmt.join(InventoryItem, InventoryItem.element_id == Element.element_id).where(
            InventoryItem.quantity > 0
        )

    elementos = list(session.scalars(stmt).unique())
    if not elementos:
        return []

    # Sin texto de búsqueda no hay nada que puntuar: devolvemos los filtrados.
    if not query or not query.strip():
        candidatos = [Candidate(element=e, score=1.0) for e in elementos]
    else:
        formas = vocab.expand_shape_terms(query)
        dims = vocab.extract_dimensions(query)
        libres = [t for t in vocab.tokens(query) if not t.isdigit()]
        candidatos = []
        for element in elementos:
            score, razones = _score_part(element.part, formas, dims, libres)
            if score > 0:
                candidatos.append(Candidate(element=element, score=score, reasons=razones))
        # Si nada puntuó, es mejor devolver el conjunto filtrado que nada.
        if not candidatos:
            candidatos = [Candidate(element=e, score=0.5) for e in elementos]

    # Bonus por el orden de preferencia del color ("rojo" prefiere Bright Red).
    if colores_preferidos:
        prioridad = {nombre: i for i, nombre in enumerate(colores_preferidos)}
        for cand in candidatos:
            posicion = prioridad.get(cand.element.color.name)
            if posicion is not None:
                cand.score += max(0.0, 2.0 - posicion * 0.5)

    candidatos.sort(key=lambda c: (-c.score, c.element.part.name))
    return candidatos[:limit]


def stock_by_element(session: Session, element_ids: list[str]) -> dict[str, int]:
    """Unidades totales en inventario de cada elemento (sumando ubicaciones)."""
    if not element_ids:
        return {}
    filas = session.execute(
        select(InventoryItem.element_id, func.sum(InventoryItem.quantity))
        .where(InventoryItem.element_id.in_(element_ids))
        .group_by(InventoryItem.element_id)
    ).all()
    return {element_id: int(total or 0) for element_id, total in filas}


def candidates_to_dicts(session: Session, candidatos: list[Candidate]) -> list[dict[str, Any]]:
    stock = stock_by_element(session, [c.element.element_id for c in candidatos])
    return [c.to_dict(quantity=stock.get(c.element.element_id, 0)) for c in candidatos]


def catalog_stats(session: Session) -> dict[str, int]:
    return {
        "colores": session.scalar(select(func.count(Color.id))) or 0,
        "piezas": session.scalar(select(func.count(Part.design_id))) or 0,
        "elementos": session.scalar(select(func.count(Element.element_id))) or 0,
    }
