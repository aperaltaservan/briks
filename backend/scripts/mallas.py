"""Genera la malla 3D de cada molde del catálogo.

    python -m scripts.mallas [--forzar]

Descarga de la biblioteca LDraw lo que haga falta y deja el resultado cacheado
en `data/mallas/`. La primera vez tarda (hay que traerse cada pieza y sus
primitivas); después el diseñador las sirve al instante.

Las piezas que LDraw no tenga con ese número se quedan sin malla y se dibujan
como la caja que ocupan: es una degradación prevista, no un fallo.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from sqlalchemy import select  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.models import Element, Part  # noqa: E402
from app.services import ldraw  # noqa: E402


def main() -> int:
    forzar = "--forzar" in sys.argv

    with session_scope() as s:
        moldes = [
            (p.design_id, p.name)
            for p in s.scalars(select(Part).order_by(Part.design_id))
            if s.scalar(select(Element.element_id).where(Element.design_id == p.design_id).limit(1))
        ]

    print(f"{len(moldes)} moldes en el catálogo.\n")
    hechas = cacheadas = sin_malla = 0
    faltan: list[tuple[str, str]] = []
    inicio = time.time()

    for design_id, nombre in moldes:
        if not forzar and ldraw.hay_malla(design_id):
            cacheadas += 1
            continue
        t0 = time.time()
        datos = ldraw.malla_o_nada(design_id)
        if datos:
            hechas += 1
            print(
                f"  ok    {design_id:10s} {nombre[:38]:38s} "
                f"{datos['triangulos']:6d} tri  {time.time() - t0:5.1f}s"
            )
        else:
            sin_malla += 1
            faltan.append((design_id, nombre))
            print(f"  --    {design_id:10s} {nombre[:38]:38s} sin equivalente en LDraw")

    print(
        f"\n{hechas} generadas, {cacheadas} ya estaban, {sin_malla} sin malla "
        f"({time.time() - inicio:.0f}s)"
    )
    if faltan:
        print("\nSe dibujarán como caja aproximada:")
        for design_id, nombre in faltan:
            print(f"  {design_id:10s} {nombre}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
