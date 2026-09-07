"""Importación de inventarios de sets desde los CSV de LEGO.com.

Formato esperado (export de LEGO Customer Service / Pick a Brick):
    SetNumber, ElementID, Qty, Colour, Category, DesignID, ElementName,
    ImageURL, ElementSetCount
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import Color, Element, LegoSet, Part, SetPart
from . import catalog

COLUMNAS_REQUERIDAS = {"ElementID", "Qty", "Colour", "DesignID", "ElementName"}


def fix_mojibake(texto: str) -> str:
    """Repara el doble encoding de los CSV de LEGO ('45Â°' -> '45°').

    Los ficheros son UTF-8 válido, pero el texto que contienen ya venía mal
    decodificado en origen (UTF-8 leído como cp1252).
    """
    if not texto or not any(marca in texto for marca in ("Â", "Ã", "â")):
        return texto
    try:
        return texto.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return texto


@dataclass
class ImportResult:
    set_number: str
    lineas: int = 0
    elementos_nuevos: int = 0
    piezas_nuevas: int = 0
    colores_nuevos: int = 0
    piezas_totales: int = 0
    errores: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errores is None:
            self.errores = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "set": self.set_number,
            "lineas_procesadas": self.lineas,
            "piezas_totales": self.piezas_totales,
            "nuevos_en_catalogo": {
                "colores": self.colores_nuevos,
                "moldes": self.piezas_nuevas,
                "elementos": self.elementos_nuevos,
            },
            "errores": self.errores,
        }


def _normaliza_set_number(valor: str) -> str:
    """LEGO usa '31134-1'; aceptamos también '31134' y añadimos el sufijo."""
    numero = (valor or "").strip()
    if numero and "-" not in numero:
        numero = f"{numero}-1"
    return numero


def import_set_csv(
    session: Session,
    contenido: str,
    set_number: str | None = None,
    set_name: str | None = None,
    reemplazar: bool = True,
) -> ImportResult:
    """Carga un CSV de LEGO en el catálogo y registra la composición del set.

    Importar un set NO añade piezas al inventario: sólo describe de qué se
    compone. El alta en inventario es un paso aparte y explícito.
    """
    lector = csv.DictReader(io.StringIO(contenido))
    if not lector.fieldnames:
        raise ValueError("El CSV está vacío o no tiene cabecera.")

    faltantes = COLUMNAS_REQUERIDAS - set(lector.fieldnames)
    if faltantes:
        raise ValueError(
            f"Al CSV le faltan columnas obligatorias: {', '.join(sorted(faltantes))}. "
            f"Encontradas: {', '.join(lector.fieldnames)}"
        )

    filas = list(lector)
    if not filas:
        raise ValueError("El CSV no contiene ninguna fila de datos.")

    numero = _normaliza_set_number(set_number or filas[0].get("SetNumber", ""))
    if not numero:
        raise ValueError("No se ha podido determinar el número de set.")

    resultado = ImportResult(set_number=numero)

    lego_set = session.get(LegoSet, numero)
    if lego_set is None:
        lego_set = LegoSet(set_number=numero, name=set_name)
        session.add(lego_set)
        session.flush()
    elif set_name:
        lego_set.name = set_name

    if reemplazar:
        session.execute(delete(SetPart).where(SetPart.set_number == numero))
        session.flush()

    acumulado: dict[str, int] = {}

    for indice, fila in enumerate(filas, start=2):
        element_id = (fila.get("ElementID") or "").strip()
        design_id = (fila.get("DesignID") or "").strip()
        if not element_id or not design_id:
            resultado.errores.append(f"Línea {indice}: falta ElementID o DesignID.")
            continue

        try:
            cantidad = int(float((fila.get("Qty") or "1").strip() or 1))
        except ValueError:
            resultado.errores.append(f"Línea {indice}: cantidad no numérica ({fila.get('Qty')!r}).")
            continue
        if cantidad <= 0:
            continue

        nombre_color = fix_mojibake((fila.get("Colour") or "Desconocido").strip())
        nombre_pieza = fix_mojibake((fila.get("ElementName") or design_id).strip())
        categoria = fix_mojibake((fila.get("Category") or "").strip()) or None
        imagen = (fila.get("ImageURL") or "").strip() or None

        color_previo = session.scalar(select(Color).where(Color.name == nombre_color))
        color = catalog.get_or_create_color(session, nombre_color)
        if color_previo is None:
            resultado.colores_nuevos += 1

        if session.get(Part, design_id) is None:
            resultado.piezas_nuevas += 1
        catalog.get_or_create_part(session, design_id, nombre_pieza, categoria)

        if session.get(Element, element_id) is None:
            resultado.elementos_nuevos += 1
        catalog.get_or_create_element(session, element_id, design_id, color.id, imagen)

        acumulado[element_id] = acumulado.get(element_id, 0) + cantidad
        resultado.lineas += 1

    for element_id, cantidad in acumulado.items():
        session.add(
            SetPart(set_number=numero, element_id=element_id, quantity=cantidad, is_spare=False)
        )
        resultado.piezas_totales += cantidad

    session.flush()
    return resultado


def import_set_csv_file(session: Session, ruta: Path, **kwargs: Any) -> ImportResult:
    contenido = ruta.read_text(encoding="utf-8-sig")
    return import_set_csv(session, contenido, **kwargs)
