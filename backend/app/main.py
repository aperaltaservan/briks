"""Aplicación FastAPI: API REST, servidor MCP y frontend en un solo proceso."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .auth import mcp_autorizado, ruta_publica
from .auth import router as auth_router
from .config import settings
from .db import init_db
from .mcp_server import mcp
from .routers.api import router as api_router

# El MCP se sirve montado en /mcp. La ruta interna es "/" porque el prefijo
# lo aporta el mount.
mcp_app = mcp.streamable_http_app(streamable_http_path="/", stateless_http=True)


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
app.mount("/mcp", mcp_app)

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
    ruta = request.url.path
    if ruta == "/mcp" or ruta.startswith("/mcp/"):
        if not mcp_autorizado(request):
            return JSONResponse({"detail": "Token MCP inválido o ausente"}, status_code=401)
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
