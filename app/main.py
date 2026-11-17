"""API HTTP de Melvara Server."""

from __future__ import annotations

import hmac
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
from .extras import ExtraLists
from .importer import ImportJobs
from .reports import Reports
from .wishlist import Wishlist

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("melvara")
logging.getLogger("httpx").setLevel(logging.WARNING)

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class State:
    service_token: str = ""
    auth: Auth
    limiter: RateLimiter
    wishlist: Wishlist
    lastfm: LastFM
    reports: Reports | None = None
    extras: ExtraLists | None = None
    imports: ImportJobs | None = None


state = State()


def configure(auth: Auth, limiter: RateLimiter, wishlist: Wishlist, lastfm: LastFM, service_token: str = "",
              reports: Reports | None = None, extras: ExtraLists | None = None,
              imports: ImportJobs | None = None) -> None:
    state.auth, state.limiter, state.wishlist, state.lastfm = auth, limiter, wishlist, lastfm
    state.reports = reports
    state.extras = extras
    state.imports = imports
    state.service_token = service_token.strip()


def _database(data_dir: str) -> str:
    """melvara.sqlite. Antes se llamaba sonora.sqlite: si solo está la vieja, se renombra."""
    path = os.path.join(data_dir, "melvara.sqlite")
    old = os.path.join(data_dir, "sonora.sqlite")
    if not os.path.exists(path) and os.path.exists(old):
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(old + suffix):
                os.replace(old + suffix, path + suffix)
        log.info("Base de datos renombrada: sonora.sqlite → melvara.sqlite")
    return path


@asynccontextmanager
async def lifespan(app):
    if not getattr(state, "wishlist", None):  # en las pruebas se configura a mano
        data_dir = os.environ.get("DATA_DIR", "/data")
        os.makedirs(data_dir, exist_ok=True)
        database = _database(data_dir)
        configure(
            Auth(os.environ["NAVIDROME_URL"]),
            RateLimiter(int(os.environ.get("RATE_LIMIT_PER_MINUTE", "120"))),
            Wishlist(database),
            LastFM(os.environ.get("LASTFM_API_KEY", ""), os.environ.get("LASTFM_SHARED_SECRET", "")),
            os.environ.get("SERVICE_TOKEN", ""),
            Reports(database),
            ExtraLists(database),
            ImportJobs(database, os.environ["NAVIDROME_URL"]),
        )
        if state.service_token and len(state.service_token) < 32:
            log.warning("SERVICE_TOKEN tiene menos de 32 caracteres: la ruta de servicio queda desactivada")
            state.service_token = ""
        log.info("Melvara Server %s listo", __version__)
    yield


MAX_BATCH = 500


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=SECURITY_HEADERS)


def protected(handler):
    """Comprueba el acceso de Navidrome y el límite de peticiones antes de responder."""

    async def endpoint(request: Request):
        h = request.headers
        # X-Melvara-*; las versiones anteriores de la app mandan X-Sonora-* (se aceptan las dos).
        user, token, salt = (h.get(f"x-melvara-{k}") or h.get(f"x-sonora-{k}", "") for k in ("user", "token", "salt"))
        client = request.client.host if request.client else "?"
        if not state.limiter.allow(f"u:{user.lower()}" if user else f"ip:{client}"):
            return _error(429, "Demasiadas peticiones")
        if not user or not token or not salt:
            return _error(401, "Falta el acceso de Navidrome")
        username = state.auth.user_for(user, token, salt)
        if username is None:
            return _error(401, "Este acceso no es válido en Navidrome")
        request.state.access = (user, token, salt)
        try:
            result = await handler(request, username)
        except PermissionError as e:
            return _error(403, str(e) or "Solo para administradores")
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
    # Público (sin datos): la app lo usa para saber que aquí hay un Melvara Server.
    return JSONResponse({"service": "melvara-api", "version": __version__, "lastfm": state.lastfm.enabled,
                         "lastfmAuth": state.lastfm.can_sign, "serviceApi": bool(state.service_token),
                         "reports": state.reports is not None, "extraLists": state.extras is not None,
                         "imports": state.imports is not None},
                        headers=SECURITY_HEADERS)


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
    kind = request.query_params.get("kind", "").strip().lower() or None
    value = request.query_params.get("value", "").strip() or None
    if kind:
        kind, value, _ = ExtraLists.clean(kind, value or "", "")
    return {"tracks": await state.lastfm.charts(country, kind=kind, value=value)}


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


def _service_denied(request: Request) -> JSONResponse | None:
    """Ruta de servicio: existe solo con SERVICE_TOKEN, tiene su límite y pide el token (Bearer)."""
    if not state.service_token:
        return _error(404, "No encontrado")
    if not state.limiter.allow("service"):
        return _error(429, "Demasiadas peticiones")
    auth = request.headers.get("authorization", "")
    given = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    if not given or not hmac.compare_digest(given.encode(), state.service_token.encode()):
        return _error(401, "Token de servicio ausente o incorrecto")
    return None


async def service_wishlist(request: Request):
    """Para aplicaciones externas: los deseos de todos los usuarios, por páginas, con su estado.
    Protegida con SERVICE_TOKEN (cabecera Authorization: Bearer). Sin token configurado, no existe (404)."""
    if (denied := _service_denied(request)) is not None:
        return denied
    try:
        after = int(request.query_params.get("after", "0"))
        limit = int(request.query_params.get("limit", "200"))
    except ValueError:
        return _error(400, "after y limit deben ser números enteros")
    if after < 0 or not 1 <= limit <= 500:
        return _error(400, "after debe ser ≥ 0 y limit estar entre 1 y 500")
    items = state.wishlist.all_after(after, limit)
    return JSONResponse({"items": items, "next": items[-1]["id"] if items else None}, headers=SECURITY_HEADERS)


async def service_wishlist_item(request: Request):
    """Cambia el estado de un deseo: PATCH {"status": "downloading"|"in_library"|…, "note": "…"} (con el token)."""
    if (denied := _service_denied(request)) is not None:
        return denied
    try:
        item_id = int(request.path_params["id"])
        body = json.loads(await request.body() or b"{}")
    except (ValueError, json.JSONDecodeError):
        return _error(400, "Datos no válidos")
    if not isinstance(body, dict) or not isinstance(body.get("status"), str):
        return _error(400, "Falta status")
    note = body.get("note")
    try:
        item = state.wishlist.set_status(item_id, body["status"], str(note) if note is not None else None)
    except ValueError as e:
        return _error(400, str(e))
    if item is None:
        return _error(404, "No encontrado")
    log.info("Deseo %d: %s (ruta de servicio)", item_id, item["status"])
    return JSONResponse({"item": item}, headers=SECURITY_HEADERS)


async def wishlist(request: Request, user: str):
    if request.method == "GET":
        return {"items": state.wishlist.list(user)}
    try:
        body = json.loads(await request.body() or b"{}")
    except json.JSONDecodeError:
        raise ValueError("Datos no válidos")
    if not isinstance(body, dict):
        raise ValueError("Datos no válidos")

    def add(entry: dict) -> dict:
        year = entry.get("year")
        album = entry.get("album")
        return state.wishlist.add(user, str(entry.get("title", "")), str(entry.get("artist", "")),
                                  str(album) if album is not None else None, year if isinstance(year, int) else None)

    # Varias de una vez (p. ej. las no encontradas al importar): {"items": [{title, artist, album?, year?}, …]}.
    if "items" in body:
        entries = body["items"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_BATCH:
            raise ValueError(f"items debe ser una lista de 1 a {MAX_BATCH} canciones")
        items, skipped = [], 0
        for entry in entries:
            try:
                items.append(add(entry if isinstance(entry, dict) else {}))
            except ValueError as e:
                if str(e) == "La lista está llena":
                    skipped += len(entries) - len(items) - skipped
                    break
                skipped += 1
        return {"items": items, "skipped": skipped}
    return {"item": add(body)}


async def wishlist_item(request: Request, user: str):
    try:
        item_id = int(request.path_params["id"])
    except ValueError:
        raise ValueError("Identificador no válido")
    return {"deleted": True} if state.wishlist.remove(user, item_id) else None


# ------------------------------------------------------------------ denuncias


def _reports() -> Reports:
    if state.reports is None:
        raise ValueError("Las denuncias no están activadas en este servidor")
    return state.reports


def _require_admin(request: Request) -> None:
    if not state.auth.is_admin(*request.state.access):
        raise PermissionError("Solo las cuentas administradoras de Navidrome ven las denuncias")


async def reports(request: Request, user: str):
    if request.method == "GET":
        _require_admin(request)
        return {"items": _reports().list(request.query_params.get("all") == "1")}
    try:
        body = json.loads(await request.body() or b"{}")
    except json.JSONDecodeError:
        raise ValueError("Datos no válidos")
    if not isinstance(body, dict):
        raise ValueError("Datos no válidos")

    def text(key: str) -> str | None:
        value = body.get(key)
        return str(value) if value is not None else None

    item = _reports().add(user, text("reported") or "", text("reason") or "", text("details"),
                          text("playlistId"), text("playlistName"))
    log.info("Denuncia %d: %s denuncia a %s (%s)", item["id"], user, item["reported"], item["reason"])
    return {"item": item}


async def report_item(request: Request, user: str):
    _require_admin(request)
    try:
        item_id = int(request.path_params["id"])
    except ValueError:
        raise ValueError("Identificador no válido")
    item = _reports().resolve(item_id, user)
    return {"item": item} if item else None


# ------------------------------------------------------------------ listas extra


def _extras() -> ExtraLists:
    if state.extras is None:
        raise ValueError("Las listas extra no están activadas en este servidor")
    return state.extras


async def _body(request: Request) -> dict:
    try:
        body = json.loads(await request.body() or b"{}")
    except json.JSONDecodeError:
        raise ValueError("Datos no válidos")
    if not isinstance(body, dict):
        raise ValueError("Datos no válidos")
    return body


async def _check_lastfm(kind: str, value: str) -> None:
    """Antes de guardar una lista: que Last.fm la tenga y no esté vacía."""
    if not state.lastfm.enabled:
        raise ValueError("Este servidor no tiene configurado Last.fm")
    if not await state.lastfm.charts(None, limit=5, kind=kind, value=value):
        raise ValueError("Last.fm no tiene canciones para esa lista")


async def extra_lists(request: Request, user: str):
    if request.method == "GET":
        return {"items": _extras().list()}
    _require_admin(request)
    body = await _body(request)
    kind, value, title = ExtraLists.clean(str(body.get("kind", "")), str(body.get("value", "")), str(body.get("title", "")))
    await _check_lastfm(kind, value)
    item = _extras().add(kind, value, title, user)
    log.info("Lista extra %d: %s %s («%s») por %s", item["id"], kind, value, title, user)
    return {"item": item}


async def extra_list_item(request: Request, user: str):
    _require_admin(request)
    try:
        item_id = int(request.path_params["id"])
    except ValueError:
        raise ValueError("Identificador no válido")
    if request.method == "DELETE":
        return {"deleted": True} if _extras().remove(item_id) else None
    body = await _body(request)
    kind, value, title = ExtraLists.clean(str(body.get("kind", "")), str(body.get("value", "")), str(body.get("title", "")))
    current = _extras().get(item_id)
    if current and (current["kind"], current["value"]) != (kind, value):
        await _check_lastfm(kind, value)
    item = _extras().update(item_id, kind, value, title)
    return {"item": item} if item else None


# ------------------------------------------------------------------ importar en el servidor


def _imports() -> ImportJobs:
    if state.imports is None:
        raise ValueError("Este servidor no importa en segundo plano")
    return state.imports


async def import_jobs(request: Request, user: str):
    if request.method == "GET":
        job = _imports().latest(user)
        return {"job": job}
    return {"job": _imports().start(user, request.state.access, await _body(request))}


async def import_job(request: Request, user: str):
    try:
        job_id = int(request.path_params["id"])
    except ValueError:
        raise ValueError("Identificador no válido")
    if request.method == "DELETE":
        job = _imports().cancel(job_id, user)
    else:
        job = _imports().get(job_id, user)
    return {"job": job} if job else None


app = Starlette(
    routes=[
        Route("/health", health),
        Route("/api/v1/status", status),
        Route("/api/v1/lastfm/search", protected(search)),
        Route("/api/v1/lastfm/charts", protected(charts)),
        Route("/api/v1/lastfm/call", protected(lastfm_call), methods=["POST"]),
        Route("/api/v1/wishlist", protected(wishlist), methods=["GET", "POST"]),
        Route("/api/v1/wishlist/{id}", protected(wishlist_item), methods=["DELETE"]),
        Route("/api/v1/reports", protected(reports), methods=["GET", "POST"]),
        Route("/api/v1/charts/extra", protected(extra_lists), methods=["GET", "POST"]),
        Route("/api/v1/import/jobs", protected(import_jobs), methods=["GET", "POST"]),
        Route("/api/v1/import/jobs/{id}", protected(import_job), methods=["GET", "DELETE"]),
        Route("/api/v1/charts/extra/{id}", protected(extra_list_item), methods=["PATCH", "DELETE"]),
        Route("/api/v1/reports/{id}", protected(report_item), methods=["PATCH"]),
        Route("/api/v1/service/wishlist", service_wishlist, methods=["GET"]),
        Route("/api/v1/service/wishlist/{id}", service_wishlist_item, methods=["PATCH"]),
    ],
    lifespan=lifespan,
)
