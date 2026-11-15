"""API HTTP de Sonora Server."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from . import __version__, text
from .auth import Auth, RateLimiter
from .catalog import Catalog
from .plex import Plex
from .sync import Syncer

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("sonora")
# Una línea por cada petición a Plex es demasiado ruido (y no aporta nada a quien lo despliega).
logging.getLogger("httpx").setLevel(logging.WARNING)

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
MAX_LIMIT = 200


class State:
    catalog: Catalog
    syncer: Syncer
    auth: Auth
    limiter: RateLimiter


state = State()


def configure(catalog: Catalog, auth: Auth, limiter: RateLimiter, syncer: Syncer | None) -> None:
    state.catalog = catalog
    state.auth = auth
    state.limiter = limiter
    state.syncer = syncer


@asynccontextmanager
async def lifespan(app):
    if not getattr(state, "catalog", None):  # en las pruebas se configura a mano
        plex_url = os.environ["PLEX_URL"]
        plex_token = os.environ["PLEX_TOKEN"]
        data_dir = os.environ.get("DATA_DIR", "/data")
        os.makedirs(data_dir, exist_ok=True)
        catalog = Catalog(os.path.join(data_dir, "catalog.sqlite"))
        client_id = catalog.meta("clientId")
        if not client_id:
            import uuid
            client_id = f"sonora-server-{uuid.uuid4()}"
            catalog.set_meta("clientId", client_id)
        plex = Plex(plex_url, plex_token, client_id)
        syncer = Syncer(catalog, plex, int(os.environ.get("SYNC_INTERVAL_MINUTES", "30")))
        configure(catalog, Auth(plex_url), RateLimiter(int(os.environ.get("RATE_LIMIT_PER_MINUTE", "600"))), syncer)
        syncer.start()
        log.info("Sonora Server %s listo", __version__)
    yield


# ------------------------------------------------------------------ ayudas


def _token(request: Request) -> str:
    token = request.headers.get("x-plex-token", "")
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    return token


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=SECURITY_HEADERS)


def _limit(request: Request, name: str, default: int) -> int:
    try:
        return max(1, min(int(request.query_params.get(name, default)), MAX_LIMIT))
    except ValueError:
        return default


def protected(handler):
    """Comprueba el acceso de Plex y el límite de peticiones antes de responder."""

    def endpoint(request: Request):
        token = _token(request)
        client = request.client.host if request.client else "?"
        if not state.limiter.allow(Auth.key(token) if token else f"ip:{client}"):
            return _error(429, "Demasiadas peticiones")
        if not token:
            return _error(401, "Falta el acceso de Plex")
        sections = state.auth.sections_for(token)
        if sections is None:
            return _error(401, "Este acceso de Plex no tiene permiso en este servidor")
        try:
            result = handler(request, sections)
        except ValueError:
            return _error(400, "Petición no válida")
        if result is None:
            return _error(404, "No encontrado")
        return JSONResponse(result, headers=SECURITY_HEADERS)

    return endpoint


def _path_id(request: Request) -> int:
    return int(request.path_params["id"])


# ------------------------------------------------------------------ rutas


def health(request: Request):
    return PlainTextResponse("ok")


@protected
def status(request, sections):
    s = state.syncer
    synced = state.catalog.meta("syncedAt")
    return {
        "version": __version__,
        "ready": synced is not None,
        "syncedAt": int(float(synced)) if synced else None,
        "syncing": bool(s and s.syncing),
        "progress": round(s.progress, 3) if s else 1.0,
        "counts": state.catalog.counts(),
    }


@protected
def search(request, sections):
    q = request.query_params.get("q", "")[:200]
    if not text.tokens(q):
        return {"top": None, "tracks": [], "albums": [], "artists": []}
    return state.catalog.search(q, sections)


@protected
def tracks(request, sections):
    raw = request.query_params.get("ids", "")
    ids = [int(x) for x in raw.split(",") if x.strip()][:500]
    return {"tracks": state.catalog.tracks_by_ids(ids, sections)}


@protected
def random_tracks(request, sections):
    return {"tracks": state.catalog.random_tracks(_limit(request, "n", 50), sections)}


@protected
def album(request, sections):
    return state.catalog.album(_path_id(request), sections)


@protected
def album_tracks(request, sections):
    return {"tracks": state.catalog.tracks_in_album(_path_id(request), sections)}


@protected
def newest_albums(request, sections):
    return {"albums": state.catalog.newest_albums(_limit(request, "limit", 40), sections)}


@protected
def random_albums(request, sections):
    return {"albums": state.catalog.random_albums(_limit(request, "n", 4), sections)}


@protected
def artist(request, sections):
    return state.catalog.artist(_path_id(request), sections)


@protected
def artist_albums(request, sections):
    return {"albums": state.catalog.albums_by_artist(_path_id(request), sections)}


@protected
def artist_tracks(request, sections):
    limit = min(_limit(request, "limit", 200), MAX_LIMIT)
    return {"tracks": state.catalog.tracks_by_artist(_path_id(request), limit, sections)}


@protected
def related_artists(request, sections):
    names = {text.norm(n) for n in request.query_params.get("names", "").split("|") if n.strip()}
    genres = {text.fold(g) for g in request.query_params.get("genres", "").split("|") if g.strip()}
    exclude = request.query_params.get("exclude")
    return {"artists": state.catalog.related_artists(
        set(list(names)[:50]), set(list(genres)[:10]), int(exclude) if exclude else None,
        _limit(request, "limit", 30), sections)}


routes = [
    Route("/health", health),
    Route("/api/v1/status", status),
    Route("/api/v1/search", search),
    Route("/api/v1/tracks", tracks),
    Route("/api/v1/tracks/random", random_tracks),
    Route("/api/v1/albums/newest", newest_albums),
    Route("/api/v1/albums/random", random_albums),
    Route("/api/v1/albums/{id:int}", album),
    Route("/api/v1/albums/{id:int}/tracks", album_tracks),
    Route("/api/v1/artists/related", related_artists),
    Route("/api/v1/artists/{id:int}", artist),
    Route("/api/v1/artists/{id:int}/albums", artist_albums),
    Route("/api/v1/artists/{id:int}/tracks", artist_tracks),
]

app = Starlette(routes=routes, lifespan=lifespan)
