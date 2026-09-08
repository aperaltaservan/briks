"""Aplicación FastAPI: API REST, servidor MCP y frontend en un solo proceso."""
from __future__ import annotations

from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.auth.routes import (
    MetadataHandler,
    ProtectedResourceMetadataHandler,
    build_metadata,
    build_resource_metadata_url,
    create_auth_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import ProtectedResourceMetadata
from pydantic import AnyHttpUrl
from starlette.middleware.sessions import SessionMiddleware
from starlette.routing import Route

from .auth import mcp_autorizado, ruta_publica
from .auth import router as auth_router
from .config import settings
from .db import init_db
from .mcp_server import mcp
from .oauth import SCOPE, proveedor
from .oauth import router as oauth_router
from .routers.api import router as api_router

# URL de los metadatos del recurso protegido: es lo que se le dice al cliente
# en la cabecera WWW-Authenticate para que sepa dónde negociar el token.
_RECURSO_MCP = (
    AnyHttpUrl(f"{settings.public_url.rstrip('/')}/mcp") if settings.public_url else None
)
_URL_METADATOS = str(build_resource_metadata_url(_RECURSO_MCP)) if _RECURSO_MCP else None

# El SDK protege el transporte contra DNS rebinding y, si no se le dice nada,
# da por buenos únicamente los Host de localhost. Detrás del proxy llega el
# dominio real, así que toda petición a /mcp se iba en 421 ("Invalid Host
# header") -- y justo después de que el cliente hubiera conseguido su token,
# que es lo que hacía parecer roto el OAuth. Se declaran los hosts de verdad.
_HOSTS_MCP = ["localhost", "127.0.0.1", "localhost:*", "127.0.0.1:*", "[::1]:*"]
_ORIGENES_MCP = ["http://localhost:*", "http://127.0.0.1:*"]
if settings.public_url:
    _publico = urlparse(settings.public_url)
    if _publico.netloc:
        _HOSTS_MCP += [_publico.netloc, f"{_publico.hostname}:*"]
        # Un cliente que hable desde un servidor no manda Origin y entonces no
        # se comprueba; el nuestro se admite por si la llamada sale del propio
        # sitio. Cualquier otro sigue rechazado: eso es lo que protege de que
        # una web ajena use el navegador de alguien para hablar con /mcp.
        _ORIGENES_MCP.append(f"{_publico.scheme}://{_publico.netloc}")

# El MCP se sirve montado en /mcp. La ruta interna es "/" porque el prefijo
# lo aporta el mount.
mcp_app = mcp.streamable_http_app(
    streamable_http_path="/",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_HOSTS_MCP,
        allowed_origins=_ORIGENES_MCP,
    ),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # La sub-app MCP tiene su propio lifespan (arranca el gestor de sesiones);
    # al montarla, Starlette no lo ejecuta por nosotros.
    async with mcp_app.router.lifespan_context(mcp_app):
        yield


app = FastAPI(
    title="Inventario y montajes LEGO",
    description=(
        "Inventario de piezas LEGO y creación de montajes. El mismo motor se "
        "expone como API REST y como servidor MCP en /mcp."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# Las mallas de las piezas son el grueso del tráfico y comprimen muy bien.
app.add_middleware(GZipMiddleware, minimum_size=2048)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
app.include_router(auth_router)
app.include_router(oauth_router)
app.mount("/mcp", mcp_app)

# Rutas del flujo OAuth (metadatos, registro dinámico, /authorize, /token,
# /revoke). Las pone enteras el SDK; nosotros sólo aportamos el proveedor.
# Sin LEGO_PUBLIC_URL no se publican: en local basta el token fijo.
if settings.public_url:
    _emisor = AnyHttpUrl(settings.public_url)
    _registro = ClientRegistrationOptions(
        enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
    )
    _revocacion = RevocationOptions(enabled=True)
    _RUTA_METADATOS_AS = "/.well-known/oauth-authorization-server"
    # De las rutas del SDK se aparta la de metadatos: se republica abajo con
    # un cambio. Starlette se queda con la primera que case, así que hay que
    # sustituirla, no añadir otra igual.
    app.router.routes.extend(
        _ruta_sdk
        for _ruta_sdk in create_auth_routes(
            provider=proveedor,
            issuer_url=_emisor,
            client_registration_options=_registro,
            revocation_options=_revocacion,
        )
        if getattr(_ruta_sdk, "path", None) != _RUTA_METADATOS_AS
    )
    # El SDK anuncia como métodos del /token sólo los que llevan secreto,
    # pero el registro dinámico da de alta clientes públicos: ChatGPT se
    # registra con "none" y no recibe ninguno. Un cliente que compare lo que
    # le han dado con lo que el servidor dice admitir se planta ahí, así que
    # se añade "none" a la lista.
    _metadatos_as = build_metadata(
        issuer_url=_emisor,
        service_documentation_url=None,
        client_registration_options=_registro,
        revocation_options=_revocacion,
    )
    _metadatos_as.token_endpoint_auth_methods_supported = [
        "none",
        "client_secret_post",
        "client_secret_basic",
    ]
    app.router.routes.append(
        Route(
            _RUTA_METADATOS_AS,
            endpoint=MetadataHandler(_metadatos_as).handle,
            methods=["GET", "OPTIONS"],
        )
    )
    _metadatos = ProtectedResourceMetadataHandler(
        ProtectedResourceMetadata(
            resource=_RECURSO_MCP,
            authorization_servers=[_emisor],
            scopes_supported=[SCOPE],
            resource_name="Inventario LEGO",
        )
    )
    # La ruta canónica lleva el sufijo del recurso (/...-resource/mcp); se
    # publica también la genérica porque no todos los clientes la calculan.
    for _ruta in (build_resource_metadata_url(_RECURSO_MCP).path, "/.well-known/oauth-protected-resource"):
        app.router.routes.append(Route(_ruta, endpoint=_metadatos.handle, methods=["GET", "OPTIONS"]))

# Fotos de los reconocimientos.
app.mount("/media", StaticFiles(directory=settings.uploads_dir), name="media")


@app.get("/salud", tags=["infra"])
async def salud() -> JSONResponse:
    return JSONResponse({"estado": "ok", "mcp": "/mcp/", "api": "/api", "docs": "/docs"})


@app.middleware("http")
async def exigir_sesion(request, call_next):
    """Toda la app vive detrás de un login salvo login/salud/estáticos y /mcp.

    /mcp lo usan clientes MCP (Claude Code, Claude Desktop) que no pueden
    completar un login interactivo: se protege con un token fijo en vez de
    con la cookie de sesión.
    """
    # Un preflight nunca lleva credenciales (los navegadores las omiten a
    # propósito): si lo bloqueamos aquí, CORSMiddleware -- más adentro en la
    # pila -- nunca llega a contestarlo, y el navegador da por rota la
    # petición real antes de enviarla siquiera.
    if request.method == "OPTIONS":
        return await call_next(request)
    ruta = request.url.path
    if ruta == "/mcp":
        # El transporte está montado en /mcp/ y Starlette resolvería /mcp con
        # un 307. Un cliente que no reenvíe el POST (o que descarte la
        # cabecera Authorization al seguir el redirect) se queda fuera, así
        # que se reescribe la ruta aquí en vez de devolver la redirección.
        request.scope["path"] = "/mcp/"
        request.scope["raw_path"] = b"/mcp/"
    if ruta == "/mcp" or ruta.startswith("/mcp/"):
        if not await mcp_autorizado(request):
            # Sin esta cabecera el cliente sólo ve un 401 opaco: es lo que le
            # dice dónde están los metadatos para arrancar el flujo OAuth.
            cabeceras = (
                {"WWW-Authenticate": f'Bearer resource_metadata="{_URL_METADATOS}"'}
                if _URL_METADATOS
                else None
            )
            return JSONResponse(
                {"detail": "Token MCP inválido o ausente"},
                status_code=401,
                headers=cabeceras,
            )
        return await call_next(request)
    if ruta_publica(ruta) or request.session.get("autenticado"):
        return await call_next(request)
    if ruta.startswith("/api/") or ruta in ("/docs", "/openapi.json") or ruta.startswith("/media/"):
        return JSONResponse({"detail": "No has iniciado sesión"}, status_code=401)
    return RedirectResponse("/login")


@app.middleware("http")
async def sin_cache_para_el_frontend(request, call_next):
    """El navegador tiene que revalidar el JS y el CSS en cada carga.

    Sin esta cabecera, Chrome se guarda `designer.js` durante un rato y una
    corrección del diseñador puede tardar minutos en llegar a la pantalla,
    aunque el servidor ya sirva la versión nueva. Con ETag y `no-cache` la
    petición es barata (304) y siempre está al día.
    """
    respuesta = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path == "/":
        respuesta.headers["Cache-Control"] = "no-cache, must-revalidate"
    return respuesta


app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    same_site="lax",
    https_only=settings.cookie_secure,
)

# El frontend se sirve al final para que no eclipse a /api ni /mcp.
if settings.frontend_dir.exists():
    app.mount(
        "/static", StaticFiles(directory=settings.frontend_dir), name="static"
    )

    @app.get("/login", include_in_schema=False)
    async def login_page() -> FileResponse:
        return FileResponse(settings.frontend_dir / "login.html")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(settings.frontend_dir / "index.html")
