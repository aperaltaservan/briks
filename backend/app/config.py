"""Configuración central de la aplicación."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Raíz del proyecto: .../lego  (config.py -> app -> backend -> lego)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Ajustes leídos de variables de entorno con prefijo LEGO_."""

    model_config = SettingsConfigDict(env_prefix="LEGO_", env_file=".env", extra="ignore")

    db_path: Path = PROJECT_ROOT / "data" / "lego.db"
    seed_dir: Path = PROJECT_ROOT / "seed"
    frontend_dir: Path = PROJECT_ROOT / "frontend"
    uploads_dir: Path = PROJECT_ROOT / "data" / "uploads"

    # Base del CDN de LEGO: las ImageURL de los CSV son rutas relativas.
    lego_cdn_base: str = "https://www.lego.com"

    # Biblioteca LDraw: de ahí sale la geometría real de cada molde. Es
    # opcional: sin ella las piezas se dibujan como la caja que ocupan.
    ldraw_base_url: str = (
        "https://raw.githubusercontent.com/gkjohnson/ldraw-parts-library/master/complete/ldraw"
    )

    # Rebrickable es opcional: sin clave, la app funciona sólo con catálogo local.
    rebrickable_api_key: str | None = None
    rebrickable_base: str = "https://rebrickable.com/api/v3"

    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # Autenticación: un único usuario administrador (app personal, sin
    # registro). Cambia estos tres valores por variables de entorno reales
    # antes de exponer la app a internet -- ver .env.example.
    admin_user: str = "admin"
    admin_password: str = "cambia-esta-contrasena"
    session_secret: str = "cambia-este-secreto-por-uno-aleatorio"
    # La cookie de sesión sólo debe viajar por HTTPS en producción.
    cookie_secure: bool = False
    # Token para /mcp (opcional): sin él, /mcp queda abierto como hoy en local.
    mcp_token: str | None = None

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path}"


@lru_cache
def get_settings() -> Settings:
    cfg = Settings()
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.uploads_dir.mkdir(parents=True, exist_ok=True)
    return cfg


settings = get_settings()
