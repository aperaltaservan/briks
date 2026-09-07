"""Autenticación: un único usuario administrador (app personal, sin registro).

La sesión vive en una cookie firmada (SessionMiddleware, ver main.py). El
servidor MCP no puede hacer login interactivo -- lo usan clientes como Claude
Code, no un navegador -- así que /mcp se protege aparte con un token fijo.
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .config import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Alcanzables sin sesión: la página de login, el endpoint que la procesa y el
# healthcheck que usa Dokploy para saber si el contenedor sigue vivo.
RUTAS_PUBLICAS = {"/login", "/api/auth/login", "/salud"}
# El JS/CSS del frontend no contiene datos privados; servirlo en claro evita
# tener que duplicar login.html con sus propios estilos embebidos.
PREFIJOS_PUBLICOS = ("/static/",)


class Credenciales(BaseModel):
    usuario: str
    contrasena: str


def _validas(usuario: str, contrasena: str) -> bool:
    # compare_digest en las dos comparaciones: evita filtrar por timing si el
    # usuario ya es correcto pero la contraseña no.
    return secrets.compare_digest(usuario, settings.admin_user) and secrets.compare_digest(
        contrasena, settings.admin_password
    )


@router.post("/login")
async def login(credenciales: Credenciales, request: Request) -> dict:
    if not _validas(credenciales.usuario, credenciales.contrasena):
        raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")
    request.session["autenticado"] = True
    return {"ok": True}


@router.post("/logout")
async def logout(request: Request) -> dict:
    request.session.clear()
    return {"ok": True}


@router.get("/me")
async def me(request: Request) -> dict:
    return {"autenticado": bool(request.session.get("autenticado"))}


def ruta_publica(path: str) -> bool:
    return path in RUTAS_PUBLICAS or path.startswith(PREFIJOS_PUBLICOS)


def mcp_autorizado(request: Request) -> bool:
    """Sin token configurado, /mcp queda abierto -- igual que en local hoy."""
    if not settings.mcp_token:
        return True
    return request.headers.get("authorization", "") == f"Bearer {settings.mcp_token}"
