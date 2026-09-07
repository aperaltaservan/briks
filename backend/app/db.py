"""Motor SQLite y sesiones SQLAlchemy."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
    future=True,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _record) -> None:
    """WAL permite que la API y el MCP lean a la vez sin bloquearse."""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_session() -> Iterator[Session]:
    """Dependencia FastAPI."""
    with SessionLocal() as session:
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    """Sesión transaccional para el MCP y los scripts."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# Columnas añadidas después de la primera versión. create_all() crea tablas
# nuevas pero no toca las que ya existen, así que las añadimos a mano.
_COLUMNAS_NUEVAS: tuple[tuple[str, str, str], ...] = (
    ("builds", "baseplate_w", "INTEGER NOT NULL DEFAULT 32"),
    ("builds", "baseplate_d", "INTEGER NOT NULL DEFAULT 32"),
)


def _migrar_columnas() -> None:
    """Añade columnas que falten en bases de datos creadas con versiones previas."""
    from sqlalchemy import text

    with engine.begin() as conexion:
        for tabla, columna, definicion in _COLUMNAS_NUEVAS:
            existentes = {
                fila[1] for fila in conexion.execute(text(f"PRAGMA table_info({tabla})"))
            }
            if not existentes:  # la tabla aún no existe: create_all la creará entera
                continue
            if columna not in existentes:
                conexion.execute(text(f"ALTER TABLE {tabla} ADD COLUMN {columna} {definicion}"))


def init_db() -> None:
    from . import models  # noqa: F401  (registra las tablas en el metadata)

    models.Base.metadata.create_all(engine)
    _migrar_columnas()
