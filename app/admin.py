"""Web de administración de Melvara Server: /admin.

Se entra con una cuenta **administradora de Navidrome** (la contraseña solo se usa para preguntarle a Navidrome
si es válida: no se guarda). La sesión dura 12 horas y vive en memoria (al reiniciar el servidor hay que volver
a entrar). Sin JavaScript: formularios normales, con protección contra peticiones de otras webs (cookie
SameSite=Strict, comprobación del origen y un token en cada formulario).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import logging
import secrets
import time
from urllib.parse import parse_qs, urlparse

import httpx
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from . import __version__, countries
from .auth import NavidromeUnavailable, RateLimiter
from .sources import CHART_ORDER, NAMES, SEARCH_ORDER, SourceError

log = logging.getLogger("melvara.admin")

COOKIE = "melvara_admin"
SESSION_TTL = 12 * 3600
HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    # «same-origin» y no «no-referrer»: con «no-referrer» el navegador manda «Origin: null» en los formularios.
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                               "frame-ancestors 'none'; base-uri 'none'",
}
MESSAGES = {
    "saved": "Guardado.",
    "refreshing": "Renovando las listas por detrás: recarga la página en un minuto.",
    "csrf": "El formulario había caducado. Vuelve a intentarlo.",
    "bad": "Usuario o contraseña incorrectos.",
    "notadmin": "Esa cuenta no es administradora en Navidrome.",
    "down": "Navidrome no responde ahora mismo. Inténtalo en un momento.",
    "many": "Demasiados intentos. Espera un minuto.",
    "out": "Has salido.",
}

_sessions: dict[str, tuple[str, float]] = {}
_login_limiter = RateLimiter(10)
_tasks: set = set()


def _state():
    from .main import state  # aquí para no importar en círculo
    return state


# ------------------------------------------------------------------ sesión


def _session(request: Request) -> tuple[str, str] | None:
    token = request.cookies.get(COOKIE, "")
    hit = _sessions.get(token)
    if not hit or hit[1] < time.time():
        _sessions.pop(token, None)
        return None
    return hit[0], token


def _csrf(token: str) -> str:
    return hmac.new(token.encode(), b"melvara-admin-csrf", hashlib.sha256).hexdigest()


def _same_origin(request: Request) -> bool:
    """El formulario viene de esta misma web (cabecera Origin). Sin Origin (navegadores antiguos) se acepta: el
    token del formulario y la cookie SameSite=Strict ya lo protegen."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    hosts = {request.headers.get("host", ""), request.headers.get("x-forwarded-host", "").split(",")[0].strip()}
    ok = urlparse(origin).netloc in hosts - {""}
    if not ok:
        log.warning("Web de administración: formulario con otro origen (%s; host %s)", origin, ", ".join(sorted(hosts)))
    return ok


async def _form(request: Request) -> dict[str, str]:
    body = await request.body()
    if len(body) > 20000:
        return {}
    return {k: v[-1] for k, v in parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items()}


def _go(message: str = "") -> RedirectResponse:
    return RedirectResponse(f"/admin?m={message}" if message else "/admin", status_code=303, headers=HEADERS)


# ------------------------------------------------------------------ páginas


def _page(body: str, user: str | None = None) -> HTMLResponse:
    top = (f'<form method="post" action="/admin/logout" class="who">{html.escape(user)} · '
           f'<button class="link">Salir</button></form>') if user else ""
    return HTMLResponse(f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>Melvara Server</title>
<style>
:root{{--bg:#f6f5f2;--card:#fff;--ink:#1d1b19;--mute:#6b6660;--line:#e4e0da;--acc:#7a3cff;--warn:#a15c00;--bad:#b3261e;--ok:#1b7f3b}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151413;--card:#1f1e1c;--ink:#f1eee9;--mute:#a39d95;--line:#34312d;--acc:#a982ff;--warn:#f0b35a;--bad:#ff8a80;--ok:#6fd08c}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
main{{max-width:860px;margin:0 auto;padding:24px 16px 64px}}header{{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}}
h1{{font-size:22px;margin:0}}h1 small{{color:var(--mute);font-weight:400;font-size:14px}}h2{{font-size:17px;margin:0 0 8px}}
section{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:16px 0}}
p{{margin:6px 0}}.mute{{color:var(--mute);font-size:13px}}.warn{{color:var(--warn)}}.bad{{color:var(--bad)}}.ok{{color:var(--ok)}}
label{{display:block;margin:6px 0}}input[type=text],input[type=password],select{{font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);max-width:100%}}
input[type=text],input[type=password]{{width:100%}}
button{{font:inherit;padding:8px 16px;border-radius:8px;border:0;background:var(--acc);color:#fff;cursor:pointer}}
button.second{{background:transparent;color:var(--acc);border:1px solid var(--acc)}}button.link{{background:none;color:var(--acc);padding:0;border:0}}
.who{{color:var(--mute);margin:0}}.msg{{background:var(--card);border-left:4px solid var(--acc);padding:10px 14px;border-radius:8px;margin-top:16px}}
.table{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{text-align:left;padding:7px 6px;border-top:1px solid var(--line);vertical-align:top}}
th{{color:var(--mute);font-weight:500;border-top:0}}.row{{display:flex;gap:10px;flex-wrap:wrap;align-items:center}}
</style></head><body><main><header><h1>Melvara Server <small>{__version__}</small></h1>{top}</header>{body}</main></body></html>""",
                        headers=HEADERS)


def _message(request: Request) -> str:
    text = MESSAGES.get(request.query_params.get("m", ""))
    return f'<p class="msg">{html.escape(text)}</p>' if text else ""


def _login_page(request: Request) -> HTMLResponse:
    return _page(f"""{_message(request)}
<section><h2>Entrar</h2>
<p class="mute">Con una cuenta administradora de tu Navidrome. La contraseña no se guarda.</p>
<form method="post" action="/admin/login">
<label>Usuario<input type="text" name="user" autocomplete="username" required autocapitalize="none"></label>
<label>Contraseña<input type="password" name="password" autocomplete="current-password" required></label>
<p><button>Entrar</button></p></form></section>""")


def _ago(ts: int | None) -> str:
    if not ts:
        return "nunca"
    minutes = int(time.time() - ts) // 60
    if minutes < 1:
        return "ahora"
    if minutes < 60:
        return f"hace {minutes} min"
    if minutes < 48 * 60:
        return f"hace {minutes // 60} h"
    return f"hace {minutes // 1440} días"


def _app(version: str | None) -> str:
    if not version:
        return "—"
    return f"compilación {version[1:-1]}" if version.startswith("(") else version


def _options(selected: str, names: list[str], default_label: str | None = None) -> str:
    out = [f'<option value=""{" selected" if not selected else ""}>{html.escape(default_label)}</option>'] \
        if default_label is not None else []
    for n in names:
        out.append(f'<option value="{n}"{" selected" if n == selected else ""}>{html.escape(NAMES[n])}</option>')
    return "".join(out)


def _dashboard(request: Request, user: str, token: str) -> HTMLResponse:
    st = _state()
    src = st.sources
    s = src.settings
    csrf = f'<input type="hidden" name="csrf" value="{_csrf(token)}">'
    available = {n: src.available(n) for n in CHART_ORDER}

    def missing(n: str) -> str:
        return "" if available[n] else " <span class='mute'>(no disponible)</span>"

    # Búsqueda
    search_now = src.search_source()
    search_choice = s.get("search") or "deezer"
    radios = "".join(
        f'<label><input type="radio" name="search" value="{n}"{" checked" if n == search_choice else ""}> '
        f'{NAMES[n]}{missing(n)}</label>' for n in SEARCH_ORDER)

    # Listas
    by_list = s.get("charts_by_list") or {}
    cache = src.cached()
    rows = []
    for key in countries.APP_LISTS:
        names = [n for n in CHART_ORDER if src._can(n, key)]
        entry = cache.get(key)
        if entry:
            now = (f'{html.escape(NAMES.get(entry["source"], entry["source"]))} · {len(entry["tracks"])} canciones · '
                   f'{_ago(entry["fetchedAt"])}')
            if entry.get("error"):
                now += f'<br><span class="warn mute">Último intento: {html.escape(entry["error"][:200])}</span>'
        else:
            now = '<span class="mute">Aún no se ha descargado</span>'
        rows.append(f'<tr><td>{html.escape(countries.spanish(key))}</td>'
                    f'<td><select name="list_{key}">{_options(by_list.get(key, ""), names, "Predeterminada")}</select></td>'
                    f'<td>{now}</td></tr>')
    extras = [(k, e) for k, e in cache.items() if k.startswith("x|")]
    extra_rows = "".join(
        f'<tr><td>{html.escape(k.split("|")[1])}: {html.escape(k.split("|", 2)[2])}</td>'
        f'<td>{html.escape(NAMES.get(e["source"], e["source"]))} · {len(e["tracks"])} canciones · {_ago(e["fetchedAt"])}</td></tr>'
        for k, e in sorted(extras))

    # Last.fm
    from_env = bool(src._env_key)
    from_web = bool(s.get("lastfm_key"))
    lastfm_on = s.get("lastfm_on", True)
    if from_web:
        key_text = "Clave puesta en esta web" + (" (sustituye a la del entorno)" if from_env else "") + "."
    elif from_env:
        key_text = "Clave del entorno (LASTFM_API_KEY)."
    else:
        key_text = "Sin clave."
    secret_text = ("Con secreto: las escuchas pueden ir a Last.fm." if (s.get("lastfm_secret") or src._env_secret)
                   else "Sin secreto: las escuchas no pueden ir a Last.fm a través del servidor.")
    lastfm_check = st.lastfm_check or ""

    people = ""
    if st.social is not None:
        prow = "".join(
            f'<tr><td>{html.escape(p["user"])}{" <span class=mute>(oculto)</span>" if p["hidden"] else ""}</td>'
            f'<td>{_ago(p["lastSeen"])}</td><td>{html.escape(_app(p["app"]))}</td></tr>' for p in st.social.everyone())
        people = (f'<section><h2>Personas</h2><p class="mute">Quién ha usado Melvara con este servidor, cuándo fue la '
                  f'última vez y con qué versión de la app (las anteriores a la 52 solo dicen la compilación).</p>'
                  f'<div class="table"><table><tr><th>Usuario</th><th>Último uso</th><th>App</th></tr>{prow}</table></div>'
                  f'</section>')
    body = f"""{_message(request)}
<form method="post" action="/admin/settings">{csrf}
<section><h2>Buscar canciones</h2>
<p class="mute">Para «Internet» en el buscador de la app y la lista de deseos. Ahora: <b>{html.escape(NAMES.get(search_now or "", "ninguna"))}</b>.</p>
{radios}</section>

<section><h2>Listas de éxitos</h2>
<p class="mute">Cada lista se descarga en el servidor, se guarda y se renueva cada 6 horas. Si su fuente falla,
se usa la siguiente que tenga esa lista; si fallan todas, se sigue dando la última buena. Apple Music no tiene
lista mundial y ListenBrainz solo tiene la mundial.</p>
<label>Fuente predeterminada
<select name="charts">{_options(src.chart_default(), list(CHART_ORDER))}</select></label>
<div class="table"><table><tr><th>Lista</th><th>Fuente</th><th>Ahora</th></tr>{"".join(rows)}</table></div>
{f'<h2 style="margin-top:16px">Listas extra</h2><p class="mute">Se añaden desde la app (Administración → Listas extra). Las de etiquetas y usuarios solo existen en Last.fm; las de un artista salen de Deezer si Last.fm no está.</p><div class="table"><table>{extra_rows}</table></div>' if extra_rows else ''}
</section>

<section><h2>Last.fm</h2>
<p class="warn">Desde el 1 de diciembre de 2026, la clave de API de Last.fm solo funciona si su cuenta tiene
Last.fm Pro. Sin Pro, desactívalo: la búsqueda y las listas siguen con Deezer y Apple Music.</p>
<p class="mute">{html.escape(key_text)} {html.escape(secret_text)}</p>
{f'<p class="mute">{lastfm_check}</p>' if lastfm_check else ''}
<label><input type="checkbox" name="lastfm_on" value="1"{" checked" if lastfm_on else ""}> Usar Last.fm</label>
<label>Clave de API (vacío = no cambiar)<input type="text" name="lastfm_key" autocomplete="off" spellcheck="false"></label>
<label>Secreto compartido (vacío = no cambiar)<input type="password" name="lastfm_secret" autocomplete="new-password"></label>
<label><input type="checkbox" name="lastfm_clear" value="1"> Borrar la clave y el secreto puestos aquí{" (queda la del entorno)" if from_env else ""}</label>
</section>

<p class="row"><button>Guardar</button></p>
</form>

{people}
<form method="post" action="/admin/refresh">{csrf}
<section><h2>Renovar ahora</h2><p class="mute">Descarga otra vez todas las listas sin esperar a las 6 horas.</p>
<p><button class="second">Renovar las listas</button></p></section></form>
"""
    return _page(body, user)


# ------------------------------------------------------------------ rutas


async def admin(request: Request) -> Response:
    session = _session(request)
    if not session:
        return _login_page(request)
    return _dashboard(request, *session)


async def login(request: Request) -> Response:
    client = request.client.host if request.client else "?"
    if not _same_origin(request):
        return _go("csrf")
    if not _login_limiter.allow(f"login:{client}"):
        return _go("many")
    form = await _form(request)
    user, password = form.get("user", "").strip(), form.get("password", "")
    if not user or not password or len(user) > 100 or len(password) > 200:
        return _go("bad")
    st = _state()
    salt = secrets.token_hex(8)
    token = hashlib.md5((password + salt).encode()).hexdigest()
    try:
        name = await run_in_threadpool(st.auth.user_for, user, token, salt)
    except NavidromeUnavailable:
        return _go("down")
    if name is None:
        log.info("Web de administración: acceso incorrecto (%s)", client)
        return _go("bad")
    if not await run_in_threadpool(st.auth.is_admin, user, token, salt):
        return _go("notadmin")
    session = secrets.token_urlsafe(32)
    now = time.time()
    for k, (_, exp) in list(_sessions.items()):
        if exp < now:
            _sessions.pop(k, None)
    _sessions[session] = (name, now + SESSION_TTL)
    log.info("Web de administración: entra %s", name)
    resp = _go()
    resp.set_cookie(COOKIE, session, max_age=SESSION_TTL, path="/admin", httponly=True, samesite="strict",
                    secure=request.url.scheme == "https")
    return resp


async def logout(request: Request) -> Response:
    session = _session(request)
    if session and _same_origin(request):
        _sessions.pop(session[1], None)
    resp = _go("out")
    resp.delete_cookie(COOKIE, path="/admin")
    return resp


async def _checked(request: Request) -> tuple[str, dict] | Response:
    session = _session(request)
    if not session:
        return _go()
    form = await _form(request)
    if not _same_origin(request) or not hmac.compare_digest(form.get("csrf", ""), _csrf(session[1])):
        return _go("csrf")
    return session[0], form


async def save(request: Request) -> Response:
    checked = await _checked(request)
    if isinstance(checked, Response):
        return checked
    user, form = checked
    st = _state()
    src = st.sources
    s = src.settings
    if form.get("search") in SEARCH_ORDER:
        s.set("search", form["search"])
    if form.get("charts") in CHART_ORDER:
        s.set("charts", form["charts"])
    by_list = {}
    for key in countries.APP_LISTS:
        chosen = form.get(f"list_{key}", "")
        if chosen in CHART_ORDER and src._can(chosen, key):
            by_list[key] = chosen
    s.set("charts_by_list", by_list or None)
    s.set("lastfm_on", form.get("lastfm_on") == "1")
    if form.get("lastfm_clear") == "1":
        s.set("lastfm_key", None)
        s.set("lastfm_secret", None)
    if form.get("lastfm_key", "").strip():
        s.set("lastfm_key", form["lastfm_key"].strip()[:100])
    if form.get("lastfm_secret", "").strip():
        s.set("lastfm_secret", form["lastfm_secret"].strip()[:100])
    src.apply_lastfm()
    src.forget()
    _background(src.refresh())  # las que cambian de fuente se descargan ya, sin esperar a que alguien las abra
    st.lastfm_check = await _check_lastfm()
    log.info("Web de administración: %s cambia los ajustes", user)
    return _go("saved")


async def _check_lastfm() -> str:
    """Una llamada pequeña a Last.fm para decir en la página si la clave funciona (p. ej. sin Pro)."""
    lastfm = _state().lastfm
    if not lastfm.enabled:
        return ""
    try:
        await lastfm.charts(None, limit=1)
        return '<span class="ok">Last.fm responde bien con esta clave.</span>'
    except (httpx.HTTPError, ValueError, SourceError) as e:
        return f'<span class="bad">Last.fm no acepta la clave: {html.escape(str(e)[:200])}</span>'


def _background(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def refresh(request: Request) -> Response:
    checked = await _checked(request)
    if isinstance(checked, Response):
        return checked
    _background(_state().sources.refresh(force=True))
    return _go("refreshing")
