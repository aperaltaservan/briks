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
# El flujo OAuth lo recorre un cliente que todavía no tiene sesión: descubre
# metadatos, se registra y pide el token. La pantalla de consentimiento sí
# exige sesión, pero lo comprueba ella misma para poder volver tras el login.
PREFIJOS_OAUTH = ("/.well-known/", "/authorize", "/token", "/register", "/revoke", "/oauth/")
# El JS/CSS del frontend no contiene datos privados; servirlo en claro evita
# tener que duplicar login.html con sus propios estilos embebidos.
PREFIJOS_PUBLICOS = ("/static/",)


class Credenciales(BaseModel):
    usuario: str
    contrasena: str


def _validas(usuario: str, contrasena: str) -> bool:
    # Las dos comparaciones se ejecutan siempre, sin cortocircuito: si "and"
    # evaluara compare_digest(contrasena, ...) sólo cuando el usuario ya es
    # correcto, el tiempo de respuesta filtraría si el usuario existe antes
    # de comprobar la contraseña.
    usuario_ok = secrets.compare_digest(usuario, settings.admin_user)
    contrasena_ok = secrets.compare_digest(contrasena, settings.admin_password)
    return usuario_ok and contrasena_ok


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
    return (
        path in RUTAS_PUBLICAS
        or path.startswith(PREFIJOS_PUBLICOS)
        or path.startswith(PREFIJOS_OAUTH)
    )


async def mcp_autorizado(request: Request) -> bool:
    """Sin token configurado, /mcp queda abierto -- igual que en local hoy.

    Sólo por cabecera Authorization: un token en la URL queda en logs,
    historial del navegador y cachés intermedias.
    """
    if not settings.mcp_token:
        return True
    cabecera = request.headers.get("authorization", "")
    if secrets.compare_digest(cabecera, f"Bearer {settings.mcp_token}"):
        return True
    # Si no es el token fijo, puede ser uno emitido por OAuth a un cliente
    # que pasó por la pantalla de autorización (ChatGPT y similares).
    if cabecera.startswith("Bearer "):
        from .oauth import token_oauth_valido

        return await token_oauth_valido(cabecera[7:])
    return False
