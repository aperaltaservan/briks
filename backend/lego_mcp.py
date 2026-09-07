r"""Lanzador del servidor MCP por stdio.

Se ejecuta por ruta absoluta desde cualquier directorio:

    python P:\aperalta\lego\backend\lego_mcp.py

Python añade la carpeta de este fichero a sys.path, así que el paquete `app`
se encuentra sin depender del directorio de trabajo. Es la forma de arrancarlo
desde Claude Desktop o Claude Code, que no fijan un cwd concreto.
"""
from __future__ import annotations

from app.mcp_stdio import main

if __name__ == "__main__":
    main()
