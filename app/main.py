"""API HTTP de Sonora Server."""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from . import __version__
from .auth import Auth, RateLimiter
from .lastfm import LastFM
from .wishlist import Wishlist

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("sonora")
logging.getLogger("httpx").setLevel(logging.WARNING)

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class State:
    auth: Auth
    limiter: RateLimiter
    wishlist: Wishlist
    lastfm: LastFM


state = State()


def configure(auth: Auth, limiter: RateLimiter, wishlist: Wishlist, lastfm: LastFM) -> None:
    state.auth, state.limiter, state.wishlist, state.lastfm = auth, limiter, wishlist, lastfm


@asynccontextmanager
async def lifespan(app):
    if not getattr(state, "wishlist", None):  # en las pruebas se configura a mano
        data_dir = os.environ.get("DATA_DIR", "/data")
        os.makedirs(data_dir, exist_ok=True)
        configure(
            Auth(os.environ["NAVIDROME_URL"]),
            RateLimiter(int(os.environ.get("RATE_LIMIT_PER_MINUTE", "120"))),
            Wishlist(os.path.join(data_dir, "sonora.sqlite")),
            LastFM(os.environ.get("LASTFM_API_KEY", ""), os.environ.get("LASTFM_SHARED_SECRET", "")),
        )
        log.info("Sonora Server %s listo", __version__)
    yield


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=SECURITY_HEADERS)


def protected(handler):
    """Comprueba el acceso de Navidrome y el límite de peticiones antes de responder."""

    async def endpoint(request: Request):
        h = request.headers
        user, token, salt = h.get("x-sonora-user", ""), h.get("x-sonora-token", ""), h.get("x-sonora-salt", "")
        client = request.client.host if request.client else "?"
        if not state.limiter.allow(f"u:{user.lower()}" if user else f"ip:{client}"):
            return _error(429, "Demasiadas peticiones")
        if not user or not token or not salt:
            return _error(401, "Falta el acceso de Navidrome")
        username = state.auth.user_for(user, token, salt)
        if username is None:
            return _error(401, "Este acceso no es válido en Navidrome")
        try:
            result = await handler(request, username)
        except ValueError as e:
            return _error(400, str(e) or "Petición no válida")
        if result is None:
            return _error(404, "No encontrado")
        return JSONResponse(result, headers=SECURITY_HEADERS)

    return endpoint


# ------------------------------------------------------------------ rutas


async def health(request: Request):
    return PlainTextResponse("ok")


async def status(request: Request):
    # Público (sin datos): la app lo usa para saber que aquí hay un Sonora Server.
    return JSONResponse({"service": "sonora-api", "version": __version__, "lastfm": state.lastfm.enabled,
                         "lastfmAuth": state.lastfm.can_sign}, headers=SECURITY_HEADERS)


async def search(request: Request, user: str):
    if not state.lastfm.enabled:
        raise ValueError("Este servidor no tiene configurada la búsqueda en Last.fm")
    q = request.query_params.get("q", "").strip()
    if not q or len(q) > 200:
        raise ValueError("Búsqueda vacía o demasiado larga")
    return {"results": await state.lastfm.search(q)}


async def charts(request: Request, user: str):
    if not state.lastfm.enabled:
        raise ValueError("Este servidor no tiene configurado Last.fm")
    country = request.query_params.get("country", "").strip().lower() or None
    if country and len(country) > 60:
        raise ValueError("País no válido")
    return {"tracks": await state.lastfm.charts(country)}


async def lastfm_call(request: Request, user: str):
    """La app pide firmar una llamada de Last.fm (iniciar sesión, escuchas). La contraseña de Last.fm, si va,
    solo se reenvía a Last.fm: no se guarda ni se escribe en los registros."""
    try:
        body = json.loads(await request.body() or b"{}")
    except json.JSONDecodeError:
        raise ValueError("Datos no válidos")
    method = str(body.get("method", ""))
    params = body.get("params") or {}
    if not isinstance(params, dict) or len(params) > 300:
        raise ValueError("Datos no válidos")
    return await state.lastfm.signed(method, params)


async def wishlist(request: Request, user: str):
    if request.method == "GET":
        return {"items": state.wishlist.list(user)}
    try:
        body = json.loads(await request.body() or b"{}")
    except json.JSONDecodeError:
        raise ValueError("Datos no válidos")
    year = body.get("year")
    item = state.wishlist.add(user, str(body.get("title", "")), str(body.get("artist", "")),
                              body.get("album"), int(year) if isinstance(year, int) else None)
    return {"item": item}


async def wishlist_item(request: Request, user: str):
    try:
        item_id = int(request.path_params["id"])
    except ValueError:
        raise ValueError("Identificador no válido")
    return {"deleted": True} if state.wishlist.remove(user, item_id) else None


app = Starlette(
    routes=[
        Route("/health", health),
        Route("/api/v1/status", status),
        Route("/api/v1/lastfm/search", protected(search)),
        Route("/api/v1/lastfm/charts", protected(charts)),
        Route("/api/v1/lastfm/call", protected(lastfm_call), methods=["POST"]),
        Route("/api/v1/wishlist", protected(wishlist), methods=["GET", "POST"]),
        Route("/api/v1/wishlist/{id}", protected(wishlist_item), methods=["DELETE"]),
    ],
    lifespan=lifespan,
)
