"""OAuth 2.1 para el servidor MCP.

Algunos clientes (ChatGPT) no admiten pegar un token fijo en su conector:
siguen el flujo de autorización del propio protocolo MCP, es decir, descubren
los metadatos del servidor, se registran solos y piden un token. Todo ese
protocolo lo pone el SDK; aquí sólo está la parte propia:

  - quién es el usuario -> el único administrador de la app, el mismo del
    login web: no hay registro ni usuarios múltiples que consultar,
  - dónde se guardan clientes, códigos y tokens -> en la misma base SQLite,
    para que un redespliegue no eche abajo los conectores ya autorizados.

La pantalla de consentimiento reutiliza la sesión del login: si no la hay,
manda a /login y vuelve. El token fijo de LEGO_MCP_TOKEN sigue valiendo para
clientes que sí admiten cabecera (Claude Code), así que conviven los dos.
"""
from __future__ import annotations

import html
import json
import secrets
import time
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from .config import settings
from .db import session_scope
from .models import OAuthClient, OAuthCode, OAuthTokenRow

SCOPE = "lego"
VIDA_CODIGO = 300                      # 5 min: sólo viaja del navegador al cliente
VIDA_ACCESO = 60 * 60 * 8              # 8 h
VIDA_REFRESCO = 60 * 60 * 24 * 30      # 30 días
# La petición de autorización viaja firmada en la URL en vez de guardarse en
# la base: así no quedan filas a medias si el usuario nunca llega a aceptar.
VIDA_PETICION = 600


def _serializador() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt="oauth-consentimiento")


def _ahora() -> int:
    return int(time.time())


class ProveedorLego:
    """Implementa el proveedor OAuth del SDK sobre el usuario único de la app."""

    # ---------------------------------------------------------------- clientes
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with session_scope() as sesion:
            fila = sesion.get(OAuthClient, client_id)
            if fila is None:
                return None
            return OAuthClientInformationFull.model_validate_json(fila.data)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        with session_scope() as sesion:
            sesion.merge(
                OAuthClient(
                    client_id=client_info.client_id,
                    data=client_info.model_dump_json(),
                )
            )

    # ----------------------------------------------------------- autorización
    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Devuelve a dónde mandar el navegador: nuestra pantalla de permiso.

        Aún no se emite ningún código: primero tiene que haber una persona
        con sesión iniciada que lo acepte.
        """
        peticion = _serializador().dumps(
            {
                "client_id": client.client_id,
                "redirect_uri": str(params.redirect_uri),
                "explicita": params.redirect_uri_provided_explicitly,
                "state": params.state,
                "scopes": params.scopes or [SCOPE],
                "code_challenge": params.code_challenge,
                "resource": params.resource,
            }
        )
        return f"/oauth/autorizar?peticion={quote(peticion, safe='')}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        with session_scope() as sesion:
            fila = sesion.get(OAuthCode, authorization_code)
            if fila is None or fila.client_id != client.client_id:
                return None
            if fila.expires_at < time.time():
                sesion.delete(fila)
                return None
            return AuthorizationCode.model_validate_json(fila.data)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        with session_scope() as sesion:
            fila = sesion.get(OAuthCode, authorization_code.code)
            if fila is None:
                raise ValueError("código de autorización ya usado o caducado")
            # De un solo uso: se consume aquí pase lo que pase después.
            sesion.delete(fila)

        return self._emitir(
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            resource=authorization_code.resource,
        )

    # -------------------------------------------------------------- refresco
    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        with session_scope() as sesion:
            fila = sesion.get(OAuthTokenRow, refresh_token)
            if fila is None or fila.kind != "refresh" or fila.client_id != client.client_id:
                return None
            if fila.expires_at is not None and fila.expires_at < _ahora():
                sesion.delete(fila)
                return None
            return RefreshToken(
                token=fila.token,
                client_id=fila.client_id,
                scopes=json.loads(fila.scopes or "[]"),
                expires_at=fila.expires_at,
            )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        with session_scope() as sesion:
            fila = sesion.get(OAuthTokenRow, refresh_token.token)
            if fila is not None:
                # Rotación: el refresco usado se invalida al emitir el nuevo.
                recurso = fila.resource
                sesion.delete(fila)
            else:
                recurso = None
        return self._emitir(
            client_id=client.client_id,
            scopes=scopes or refresh_token.scopes,
            resource=recurso,
        )

    # ---------------------------------------------------------------- tokens
    async def load_access_token(self, token: str) -> AccessToken | None:
        with session_scope() as sesion:
            fila = sesion.get(OAuthTokenRow, token)
            if fila is None or fila.kind != "access":
                return None
            if fila.expires_at is not None and fila.expires_at < _ahora():
                sesion.delete(fila)
                return None
            return AccessToken(
                token=fila.token,
                client_id=fila.client_id,
                scopes=json.loads(fila.scopes or "[]"),
                expires_at=fila.expires_at,
                resource=fila.resource,
                subject=settings.admin_user,
            )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with session_scope() as sesion:
            fila = sesion.get(OAuthTokenRow, token.token)
            if fila is not None:
                sesion.delete(fila)

    async def exchange_identity_assertion(self, client, params):  # pragma: no cover
        raise NotImplementedError("esta app no usa un proveedor de identidad externo")

    # --------------------------------------------------------------- interno
    def _emitir(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        acceso = secrets.token_urlsafe(32)
        refresco = secrets.token_urlsafe(32)
        caduca = _ahora() + VIDA_ACCESO
        with session_scope() as sesion:
            sesion.add(
                OAuthTokenRow(
                    token=acceso,
                    kind="access",
                    client_id=client_id,
                    scopes=json.dumps(scopes),
                    resource=resource,
                    expires_at=caduca,
                )
            )
            sesion.add(
                OAuthTokenRow(
                    token=refresco,
                    kind="refresh",
                    client_id=client_id,
                    scopes=json.dumps(scopes),
                    resource=resource,
                    expires_at=_ahora() + VIDA_REFRESCO,
                )
            )
        return OAuthToken(
            access_token=acceso,
            token_type="Bearer",
            expires_in=VIDA_ACCESO,
            scope=" ".join(scopes),
            refresh_token=refresco,
        )


proveedor = ProveedorLego()


async def token_oauth_valido(token: str) -> bool:
    """¿Es este Bearer un token de acceso vivo? Lo usa el guardián de /mcp."""
    return await proveedor.load_access_token(token) is not None


# ---------------------------------------------------------------------------
# Pantalla de consentimiento
# ---------------------------------------------------------------------------
router = APIRouter(prefix="/oauth", tags=["oauth"])


def _leer_peticion(peticion: str) -> dict:
    try:
        return _serializador().loads(peticion, max_age=VIDA_PETICION)
    except SignatureExpired:
        raise HTTPException(status_code=400, detail="La petición ha caducado, vuelve a empezar.")
    except BadSignature:
        raise HTTPException(status_code=400, detail="Petición de autorización no válida.")


def _pagina(nombre_cliente: str, peticion: str, csrf: str) -> str:
    # El nombre del cliente lo pone quien se registra, y el registro es
    # abierto: sin escapar, cualquiera podría colar HTML en esta pantalla.
    nombre_cliente = html.escape(nombre_cliente)
    peticion = html.escape(peticion, quote=True)
    csrf = html.escape(csrf, quote=True)
    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Autorizar acceso - Inventario LEGO</title>
  <link rel="stylesheet" href="/static/styles.css" />
</head>
<body style="display:flex; align-items:center; justify-content:center; min-height:100vh;">
  <div class="panel" style="width:min(400px, 92vw);">
    <div class="marca" style="margin-bottom:14px;">
      <span class="ladrillo" aria-hidden="true"></span>
      <div>
        <h1 style="margin:0; font-size:17px;">Autorizar acceso</h1>
        <p class="sub" style="margin:0;">Inventario LEGO</p>
      </div>
    </div>
    <p style="font-size:14px; line-height:1.5;">
      <strong>{nombre_cliente}</strong> quiere conectarse a tu inventario: podrá
      consultar y modificar piezas y montajes igual que tú desde la web.
    </p>
    <p class="pista">Podrás revocarlo más adelante borrando el conector en esa aplicación.</p>
    <form method="post" action="/oauth/autorizar">
      <input type="hidden" name="peticion" value="{peticion}" />
      <input type="hidden" name="csrf" value="{csrf}" />
      <div class="fila">
        <button type="submit" name="decision" value="no" class="pequeno" style="flex:1;">Cancelar</button>
        <button type="submit" name="decision" value="si" class="primario" style="flex:2;">Autorizar</button>
      </div>
    </form>
  </div>
</body>
</html>"""


@router.get("/autorizar", include_in_schema=False)
async def pantalla_autorizar(request: Request, peticion: str):
    datos = _leer_peticion(peticion)
    if not request.session.get("autenticado"):
        # Sin sesión no hay a quién pedirle permiso: al login y de vuelta aquí.
        vuelta = quote(f"/oauth/autorizar?peticion={quote(peticion, safe='')}", safe="")
        return RedirectResponse(f"/login?siguiente={vuelta}", status_code=303)

    # Testigo anti-CSRF ligado a la sesión: sin él, bastaría con que alguien
    # llevara al usuario a enviar este formulario para autorizar su cliente.
    csrf = secrets.token_urlsafe(32)
    request.session["csrf_oauth"] = csrf

    cliente = await proveedor.get_client(datos["client_id"])
    nombre = (cliente.client_name if cliente else None) or datos["client_id"]
    return HTMLResponse(_pagina(nombre, peticion, csrf))


@router.post("/autorizar", include_in_schema=False)
async def conceder(
    request: Request,
    peticion: str = Form(...),
    decision: str = Form("no"),
    csrf: str = Form(""),
) -> RedirectResponse:
    datos = _leer_peticion(peticion)
    if not request.session.get("autenticado"):
        raise HTTPException(status_code=401, detail="Sesión caducada, vuelve a entrar.")

    esperado = request.session.pop("csrf_oauth", "")
    if not esperado or not secrets.compare_digest(csrf, esperado):
        raise HTTPException(status_code=400, detail="Petición no válida, vuelve a intentarlo.")

    destino = datos["redirect_uri"]
    parametros: dict[str, str] = {}
    if datos.get("state"):
        parametros["state"] = datos["state"]

    if decision != "si":
        parametros["error"] = "access_denied"
        parametros["error_description"] = "El usuario no autorizó el acceso"
        return RedirectResponse(f"{destino}?{urlencode(parametros)}", status_code=303)

    codigo = secrets.token_urlsafe(32)
    autorizacion = AuthorizationCode(
        code=codigo,
        scopes=datos["scopes"],
        expires_at=time.time() + VIDA_CODIGO,
        client_id=datos["client_id"],
        code_challenge=datos["code_challenge"],
        redirect_uri=AnyUrl(destino),
        redirect_uri_provided_explicitly=datos["explicita"],
        resource=datos.get("resource"),
        subject=settings.admin_user,
    )
    with session_scope() as sesion:
        sesion.add(
            OAuthCode(
                code=codigo,
                client_id=datos["client_id"],
                data=autorizacion.model_dump_json(),
                expires_at=autorizacion.expires_at,
            )
        )

    parametros["code"] = codigo
    return RedirectResponse(f"{destino}?{urlencode(parametros)}", status_code=303)
