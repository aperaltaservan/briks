#!/bin/sh
# Arranque en contenedor: siembra el catálogo la primera vez (sin base de
# datos todavía). Usa --solo-catalogo a propósito: registra los sets y sus
# piezas en el catálogo pero NO los inventaría, así que un despliegue nuevo
# arranca siempre con el inventario a cero, listo para las piezas reales.
#
# Las mallas 3D (scripts.mallas) no se generan aquí -- tardan varios minutos
# y no son obligatorias, ver README -- se pueden lanzar a mano desde la
# terminal de Dokploy si se quiere el acabado real de las piezas desde el
# primer momento.
set -e

if [ ! -f "../data/lego.db" ]; then
  echo "Primer arranque: sembrando catálogo (sin inventario) desde seed/..."
  python -m scripts.seed --solo-catalogo
fi

exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
