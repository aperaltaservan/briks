"""Inicializa la base de datos a partir de los CSV de la carpeta seed/.

    python -m scripts.seed              # importa y añade al inventario
    python -m scripts.seed --limpiar    # borra la BD y empieza de cero
    python -m scripts.seed --solo-catalogo   # importa sin tocar el inventario

Se ejecuta desde la carpeta backend/.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.services import catalog, inventory  # noqa: E402
from app.services.importers import import_set_csv_file  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Carga inicial del inventario LEGO.")
    parser.add_argument("--limpiar", action="store_true", help="borra la base de datos primero")
    parser.add_argument(
        "--solo-catalogo",
        action="store_true",
        help="importa los sets pero no añade sus piezas al inventario",
    )
    parser.add_argument(
        "--ubicacion", default="", help="ubicación donde guardar las piezas importadas"
    )
    args = parser.parse_args()

    if args.limpiar and settings.db_path.exists():
        # Se borran también los ficheros auxiliares del modo WAL.
        for sufijo in ("", "-wal", "-shm"):
            fichero = Path(str(settings.db_path) + sufijo)
            if fichero.exists():
                fichero.unlink()
        print(f"Base de datos eliminada: {settings.db_path}")

    init_db()

    ficheros = sorted(settings.seed_dir.glob("*.csv"))
    if not ficheros:
        print(f"No hay CSV en {settings.seed_dir}. Nada que importar.")
        return 1

    with session_scope() as session:
        for fichero in ficheros:
            resultado = import_set_csv_file(session, fichero)
            print(
                f"{fichero.name}: set {resultado.set_number}, "
                f"{resultado.piezas_totales} piezas en {resultado.lineas} líneas"
            )
            for error in resultado.errores:
                print(f"   aviso: {error}")

            if not args.solo_catalogo:
                alta = inventory.add_set_to_inventory(
                    session, resultado.set_number, location=args.ubicacion
                )
                print(f"   inventariado: +{alta['piezas_anadidas']} piezas")

    with session_scope() as session:
        resumen = inventory.inventory_summary(session)
        print(
            f"\nCatálogo: {catalog.catalog_stats(session)}\n"
            f"Inventario: {resumen['piezas_totales']} piezas, "
            f"{resumen['referencias_distintas']} referencias distintas."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
