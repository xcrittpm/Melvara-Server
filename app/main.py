"""API HTTP de Melvara Server."""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import asyncio
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

from . import __version__
from .auth import Auth, NavidromeUnavailable, RateLimiter
from . import admin, countries
from .lastfm import LastFM
from .sources import AppleMusic, Deezer, ListenBrainz, Settings, Sources
from .extras import ExtraLists
from .importer import ImportJobs
from .invites import InviteError, Invites, page as invite_page
from .dislikes import Dislikes
from .similar import SimilarArtists
from .jam import Jams
from .reports import Reports
from .social import Social
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
    jams: Jams = Jams()
    social: Social | None = None
    sources: Sources
    refresher = None
    lastfm_check: str | None = None
    invites: Invites | None = None
    dislikes: Dislikes | None = None
    similar: SimilarArtists | None = None
    invite_limiter = RateLimiter(10)


state = State()


def configure(auth: Auth, limiter: RateLimiter, wishlist: Wishlist, lastfm: LastFM, service_token: str = "",
              reports: Reports | None = None, extras: ExtraLists | None = None,
              imports: ImportJobs | None = None, social: Social | None = None,
              sources: Sources | None = None, invites: Invites | None = None,
              dislikes: Dislikes | None = None, similar: SimilarArtists | None = None) -> None:
    state.invites = invites
    state.similar = similar
    state.dislikes = dislikes
    state.auth, state.limiter, state.wishlist, state.lastfm = auth, limiter, wishlist, lastfm
    state.sources = sources or Sources(lastfm)
    state.social = social
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
        lastfm = LastFM(os.environ.get("LASTFM_API_KEY", ""), os.environ.get("LASTFM_SHARED_SECRET", ""))
        configure(
            Auth(os.environ["NAVIDROME_URL"]),
            RateLimiter(int(os.environ.get("RATE_LIMIT_PER_MINUTE", "120"))),
            Wishlist(database),
            lastfm,
            os.environ.get("SERVICE_TOKEN", ""),
            Reports(database),
            ExtraLists(database),
            ImportJobs(database, os.environ["NAVIDROME_URL"]),
            Social(database, os.environ["NAVIDROME_URL"]),
            Sources(lastfm, Settings(database), database, Deezer(), AppleMusic(), ListenBrainz()),
            Invites(database, os.environ["NAVIDROME_URL"]),
            Dislikes(database),
            SimilarArtists(database),
        )
        state.refresher = asyncio.get_running_loop().create_task(state.sources.keep_fresh())
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
        try:
            # En otro hilo: la consulta a Navidrome es bloqueante y, mientras esperaba, paraba a todo el servidor.
            username = await run_in_threadpool(state.auth.user_for, user, token, salt)
        except NavidromeUnavailable:
            return _error(503, "Navidrome no responde ahora mismo. Inténtalo en un momento.")
        if username is None:
            return _error(401, "Este acceso no es válido en Navidrome")
        request.state.access = (user, token, salt)
        if state.social is not None:
            state.social.seen(username, app_version(request))
        try:
            result = await handler(request, username)
        except PermissionError as e:
            return _error(403, str(e) or "Solo para administradores")
        except LookupError as e:
            return _error(404, str(e) or "No encontrado")
        except ValueError as e:
            return _error(400, str(e) or "Petición no válida")
        if result is None:
            return _error(404, "No encontrado")
        return JSONResponse(result, headers=SECURITY_HEADERS)

    return endpoint


def app_version(request: Request) -> str | None:
    """Versión de la app que hace la petición. Desde la 52 la manda en X-Melvara-App («1.3 (52)»); las anteriores
    solo la dejan ver en el User-Agent que pone iOS («Melvara/51 CFNetwork/… Darwin/…» → «(51)»)."""
    sent = request.headers.get("x-melvara-app", "").strip()
    if sent:
        return sent[:40]
    match = re.match(r"^(?:Melvara|Sonora)/(\d+(?:\.\d+)*)\s+CFNetwork/", request.headers.get("user-agent", ""))
    return f"({match.group(1)})" if match else None


async def played(request: Request, user: str):
    """La app avisa de cada canción que empieza (para «Última canción» en Administración)."""
    body = await _body(request)
    _social().last_played(user, body.get("track"))
    return {"ok": True}


# ------------------------------------------------------------------ «No me gusta» (2.13)


def _dislikes() -> Dislikes:
    if state.dislikes is None:
        raise LookupError("Este servidor no guarda «No me gusta»")
    return state.dislikes


async def dislikes(request: Request, user: str):
    """GET = los tuyos; POST {kind: track|album|artist, id, title?, artist?} = añadir."""
    if request.method == "GET":
        return {"items": _dislikes().list(user)}
    return {"item": _dislikes().add(user, await _body(request))}


async def similar_artists(request: Request, user: str):
    """GET ?name=Artista (hasta 3 veces) → {artists: {nombre: [parecidos…]}}. Para ∞ y la radio (2.14)."""
    if state.similar is None:
        raise LookupError("Este servidor no tiene artistas parecidos")
    names = [n for n in request.query_params.getlist("name") if n.strip()][:3]
    if not names:
        raise ValueError("Falta el nombre del artista")
    results = await asyncio.gather(*(state.similar.get(n) for n in names))
    return {"artists": dict(zip(names, results))}


async def dislike_item(request: Request, user: str):
    removed = _dislikes().remove(user, request.path_params["kind"], request.path_params["id"])
    return {"deleted": True} if removed else None


# ------------------------------------------------------------------ invitaciones (2.12)


def _invites() -> Invites:
    if state.invites is None:
        raise LookupError("Este servidor no tiene invitaciones")
    return state.invites


async def invites(request: Request, user: str):
    """Quien administra: GET = pendientes; POST {username, password, server} = crear (devuelve el enlace)."""
    await _require_admin(request)
    if request.method == "GET":
        return {"invites": _invites().pending()}
    body = await _body(request)
    item, code = _invites().create(str(body.get("username", "")), str(body.get("password", "")),
                                   str(body.get("server", "")), user)
    log.info("Invitación %d para %s, creada por %s", item["id"], item["username"], user)
    return {"invite": item, "code": code, "link": f"{str(request.base_url).rstrip('/')}/invitacion/{code}"}


async def invite_item(request: Request, user: str):
    await _require_admin(request)
    try:
        invite_id = int(request.path_params["id"])
    except ValueError:
        raise ValueError("Identificador no válido")
    return {"deleted": True} if _invites().cancel(invite_id) else None


def _invite_limited(request: Request) -> bool:
    client = request.client.host if request.client else "?"
    return not state.invite_limiter.allow(f"invite:{client}")


async def invite_public(request: Request):
    """Sin cuenta (aún no la tiene): POST /api/v1/invites/check {code} o /redeem {code, password}."""
    if state.invites is None:
        return _error(404, "No encontrado")
    if _invite_limited(request):
        return _error(429, "Demasiados intentos. Espera un minuto.")
    try:
        body = json.loads(await request.body() or b"{}")
    except json.JSONDecodeError:
        return _error(400, "Datos no válidos")
    if not isinstance(body, dict):
        return _error(400, "Datos no válidos")
    code = str(body.get("code", ""))
    try:
        if request.path_params["action"] == "check":
            result = state.invites.check(code)
        elif request.path_params["action"] == "redeem":
            result = await state.invites.redeem(code, str(body.get("password", "")))
        else:
            return _error(404, "No encontrado")
    except LookupError as e:
        return _error(404, str(e))
    except InviteError as e:
        return _error(400, str(e))
    return JSONResponse(result, headers=SECURITY_HEADERS)


async def invite_page_route(request: Request):
    """La página del enlace: abre Melvara con el código (no lo gasta; las vistas previas de los chats la abren)."""
    from html import escape
    from urllib.parse import quote
    code = request.path_params["code"]
    headers = {**SECURITY_HEADERS, "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"}
    if state.invites is None or _invite_limited(request):
        return HTMLResponse(invite_page("Melvara", "<h1>Invitación no disponible</h1>"), status_code=404, headers=headers)
    try:
        info = state.invites.check(code)
    except LookupError as e:
        return HTMLResponse(invite_page("Invitación a Melvara", f"<h1>Invitación no válida</h1><p>{escape(str(e))}</p>"),
                            status_code=404, headers=headers)
    base = str(request.base_url).rstrip("/")
    app_link = f"melvara://invite?api={quote(base, safe='')}&code={quote(code, safe='')}"
    host = escape(info["server"].split("://", 1)[-1])
    body = f"""<h1>Te han invitado a Melvara</h1>
<p>Para escuchar la música de <b>{host}</b> en tu iPhone, con la cuenta <b>{escape(info["username"])}</b>.</p>
<a class="button" href="{escape(app_link)}">Abrir en Melvara</a>
<a class="button second" href="https://apps.apple.com/app/id6835568116">Descargar Melvara en la App Store</a>
<p class="mute">Al abrirla, eliges tu contraseña y ya está. Si acabas de instalar la app, vuelve a esta página y toca
«Abrir en Melvara». La invitación sirve una sola vez y caduca en 7 días.</p>"""
    return HTMLResponse(invite_page("Invitación a Melvara", body), headers=headers)


async def admin_people(request: Request, user: str):
    """Para Administración en la app: quién usa Melvara en este servidor, cuándo entró y con qué versión."""
    await _require_admin(request)
    return {"people": _social().everyone()}


# ------------------------------------------------------------------ rutas


async def health(request: Request):
    return PlainTextResponse("ok")


async def status(request: Request):
    # Público (sin datos): la app lo usa para saber que aquí hay un Melvara Server.
    # «lastfm» = hay búsqueda y listas (sea de la fuente que sea): así lo entienden las apps ya instaladas.
    return JSONResponse({"service": "melvara-api", "version": __version__, "lastfm": state.sources.enabled,
                         "lastfmAuth": state.lastfm.can_sign, "sources": state.sources.status(), "admin": True,
                         "lastPlayed": state.social is not None, "invites": state.invites is not None,
                         "dislikes": state.dislikes is not None, "similar": state.similar is not None, "minApp": state.sources.settings.get("min_app"), "minAndroid": state.sources.settings.get("min_android"), "serviceApi": bool(state.service_token),
                         "reports": state.reports is not None, "extraLists": state.extras is not None,
                         "imports": state.imports is not None, "jam": True, "jamControl": True, "jamQueue": True, "social": state.social is not None},
                        headers=SECURITY_HEADERS)


async def search(request: Request, user: str):
    q = request.query_params.get("q", "").strip()
    if not q or len(q) > 200:
        raise ValueError("Búsqueda vacía o demasiado larga")
    results, source = await state.sources.search(q)
    return {"results": results, "source": source}


async def charts(request: Request, user: str):
    """Una lista de éxitos: mundial, de un país («spain» o «es») o una lista extra. Sale de la fuente que se
    eligió en la web de administración y se guarda unas horas (ver sources.py)."""
    country = request.query_params.get("country", "").strip().lower() or None
    if country and len(country) > 60:
        raise ValueError("País no válido")
    kind = request.query_params.get("kind", "").strip().lower() or None
    value = request.query_params.get("value", "").strip() or None
    if kind:
        kind, value, _ = ExtraLists.clean(kind, value or "", "")
        return await state.sources.chart(None, kind, value)
    try:
        code = countries.parse(country)
    except ValueError:
        # Un país que no está en la tabla: solo Last.fm lo entiende por su nombre.
        if not state.lastfm.enabled:
            raise
        return {"tracks": await state.lastfm.charts(country), "source": "lastfm", "updatedAt": None}
    return await state.sources.chart(code)


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


async def _require_admin(request: Request) -> None:
    if not await run_in_threadpool(state.auth.is_admin, *request.state.access):
        raise PermissionError("Solo las cuentas administradoras de Navidrome ven las denuncias")


async def reports(request: Request, user: str):
    if request.method == "GET":
        await _require_admin(request)
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
    await _require_admin(request)
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
    """Antes de guardar una lista: que exista (en Last.fm o, la de un artista, en Deezer) y no esté vacía."""
    await state.sources.check(kind, value)


async def extra_lists(request: Request, user: str):
    if request.method == "GET":
        return {"items": _extras().list()}
    await _require_admin(request)
    body = await _body(request)
    kind, value, title = ExtraLists.clean(str(body.get("kind", "")), str(body.get("value", "")), str(body.get("title", "")))
    await _check_lastfm(kind, value)
    item = _extras().add(kind, value, title, user)
    log.info("Lista extra %d: %s %s («%s») por %s", item["id"], kind, value, title, user)
    return {"item": item}


async def extra_list_item(request: Request, user: str):
    await _require_admin(request)
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


# ------------------------------------------------------------------ escuchar en grupo


async def jam_start(request: Request, user: str):
    return {"jam": state.jams.start(user)}


async def jam_item(request: Request, user: str):
    code = request.path_params["code"]
    if request.method == "DELETE":
        state.jams.end(code, user)
        return {"ended": True}
    return {"jam": state.jams.state(code, user)}


async def jam_action(request: Request, user: str):
    code, action = request.path_params["code"], request.path_params["action"]
    body = await _body(request) if request.method == "POST" else {}
    if action == "join":
        return {"jam": state.jams.join(code, user)}
    if action == "leave":
        state.jams.leave(code, user)
        return {"left": True}
    if action == "add":
        tracks = body.get("tracks")
        return state.jams.add(code, user, tracks if isinstance(tracks, list) else [], str(body.get("mode", "end")))
    if action == "pending":
        try:
            after = int(request.query_params.get("after", "0"))
        except ValueError:
            after = 0
        return state.jams.pending(code, user, after)
    if action == "state":
        queue = body.get("queue")
        state.jams.update(code, user, body.get("nowPlaying"), queue if isinstance(queue, list) else [],
                          body.get("position"), body.get("playing"))
        return {"ok": True}
    if action == "perms":
        return {"jam": state.jams.set_perms(code, user, body.get("skip"), body.get("seek"))}
    if action == "control":
        return state.jams.control(code, user, str(body.get("action", "")), body.get("position"),
                                  body.get("id"), body.get("to"))
    if action == "kick":
        return {"jam": state.jams.kick(code, user, str(body.get("user", "")))}
    return None


# ------------------------------------------------------------------ social


def _social() -> Social:
    if state.social is None:
        raise LookupError("Este servidor no tiene activada la parte social")
    return state.social


async def social_people(request: Request, user: str):
    return {"people": _social().people(user)}


async def social_visibility(request: Request, user: str):
    body = await _body(request)
    hidden = body.get("hidden")
    if not isinstance(hidden, bool):
        raise ValueError("hidden debe ser true o false")
    _social().set_hidden(user, hidden)
    return {"hidden": hidden}


async def social_listening(request: Request, user: str):
    social = _social()
    if request.method == "GET":
        try:
            profiles = await social.profiles(user, request.state.access)
        except Exception as e:  # noqa: BLE001 (Navidrome no responde: sin amigos esta vez)
            log.warning("Social: no se pudieron leer los perfiles de %s: %s", user, e)
            return {"sharing": social.sharing(user), "friends": []}
        return social.friends_listening(user, profiles)
    body = await _body(request)
    if body.get("stopped") is True:
        social.stopped(user)
    else:
        social.played(user, body.get("track"))
    return {"ok": True}


async def social_share(request: Request, user: str):
    body = await _body(request)
    share = body.get("share")
    if not isinstance(share, bool):
        raise ValueError("share debe ser true o false")
    _social().set_share(user, share)
    return {"share": share}


app = Starlette(
    routes=[
        Route("/health", health),
        Route("/admin", admin.admin, methods=["GET"]),
        Route("/admin/login", admin.login, methods=["POST"]),
        Route("/admin/logout", admin.logout, methods=["POST"]),
        Route("/admin/settings", admin.save, methods=["POST"]),
        Route("/admin/refresh", admin.refresh, methods=["POST"]),
        Route("/api/v1/status", status),
        Route("/api/v1/lastfm/search", protected(search)),
        Route("/api/v1/lastfm/charts", protected(charts)),
        Route("/api/v1/lastfm/call", protected(lastfm_call), methods=["POST"]),
        Route("/api/v1/wishlist", protected(wishlist), methods=["GET", "POST"]),
        Route("/api/v1/wishlist/{id}", protected(wishlist_item), methods=["DELETE"]),
        Route("/api/v1/reports", protected(reports), methods=["GET", "POST"]),
        Route("/api/v1/charts/extra", protected(extra_lists), methods=["GET", "POST"]),
        Route("/api/v1/import/jobs", protected(import_jobs), methods=["GET", "POST"]),
        Route("/api/v1/social/people", protected(social_people), methods=["GET"]),
        Route("/api/v1/admin/people", protected(admin_people), methods=["GET"]),
        Route("/api/v1/played", protected(played), methods=["POST"]),
        Route("/api/v1/dislikes", protected(dislikes), methods=["GET", "POST"]),
        Route("/api/v1/similar/artists", protected(similar_artists), methods=["GET"]),
        Route("/api/v1/dislikes/{kind}/{id:path}", protected(dislike_item), methods=["DELETE"]),
        Route("/api/v1/invites", protected(invites), methods=["GET", "POST"]),
        Route("/api/v1/invites/{action:str}", invite_public, methods=["POST"]),
        Route("/api/v1/invites/{id:int}", protected(invite_item), methods=["DELETE"]),
        Route("/invitacion/{code}", invite_page_route, methods=["GET"]),
        Route("/api/v1/social/visibility", protected(social_visibility), methods=["POST"]),
        Route("/api/v1/social/listening", protected(social_listening), methods=["GET", "POST"]),
        Route("/api/v1/social/listening/share", protected(social_share), methods=["POST"]),
        Route("/api/v1/jam", protected(jam_start), methods=["POST"]),
        Route("/api/v1/jam/{code}", protected(jam_item), methods=["GET", "DELETE"]),
        Route("/api/v1/jam/{code}/{action}", protected(jam_action), methods=["GET", "POST"]),
        Route("/api/v1/import/jobs/{id}", protected(import_job), methods=["GET", "DELETE"]),
        Route("/api/v1/charts/extra/{id}", protected(extra_list_item), methods=["PATCH", "DELETE"]),
        Route("/api/v1/reports/{id}", protected(report_item), methods=["PATCH"]),
        Route("/api/v1/service/wishlist", service_wishlist, methods=["GET"]),
        Route("/api/v1/service/wishlist/{id}", service_wishlist_item, methods=["PATCH"]),
    ],
    lifespan=lifespan,
)
