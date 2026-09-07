"""Arranque del servidor MCP por stdio, para Claude Desktop / Claude Code.

    python -m app.mcp_stdio

Habla con la misma base de datos SQLite que la web, así que el inventario es
el mismo se use desde donde se use.
"""
from __future__ import annotations

import asyncio

from .db import init_db
from .mcp_server import mcp


def main() -> None:
    init_db()
    asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    main()
