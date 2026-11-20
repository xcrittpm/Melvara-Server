"""De dónde salen la búsqueda de canciones y las listas de éxitos (se elige en la web de administración).

Desde el 1 de diciembre de 2026, la API de Last.fm solo funciona con una cuenta de pago (Pro). Por eso hay
otras fuentes, gratuitas y sin clave:

- **Deezer**: búsqueda y listas «Top <país>» de su cuenta oficial «Deezer Charts».
- **Apple Music**: lo más escuchado de cada país (feed público de Apple Marketing Tools). No tiene mundial.
- **ListenBrainz**: lo más escuchado de la semana por quienes usan ListenBrainz (solo mundial).
- **Last.fm**: como antes, con la clave de quien despliega el servidor.

Solo se usan título, artista y álbum (no imágenes, ni audio, ni biografías). Las listas se guardan en la base
de datos y se renuevan cada pocas horas: si una fuente falla, se prueba la siguiente y, si fallan todas, se
sigue dando la última que se descargó bien.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time

import httpx

from . import countries
from .lastfm import LastFM, clean_search

log = logging.getLogger("melvara.sources")

UA = {"User-Agent": "MelvaraServer/2 (+https://github.com/xcrittpm/Melvara)"}
NAMES = {"apple": "Apple Music", "deezer": "Deezer", "lastfm": "Last.fm", "listenbrainz": "ListenBrainz"}
CHART_ORDER = ("apple", "deezer", "lastfm", "listenbrainz")
SEARCH_ORDER = ("deezer", "lastfm")
SEARCH_TTL = 12 * 3600   # una búsqueda igual se responde de memoria durante 12 horas
SEARCH_MAX = 2000
FRESH = 6 * 3600          # una lista se renueva a las 6 horas
KEEP_REFRESHING = 14 * 86400  # se siguen renovando solas las listas que alguien abrió en las últimas 2 semanas


class SourceError(Exception):
    pass


# ------------------------------------------------------------------ ajustes


class Settings:
    """Ajustes de la web de administración (tabla `settings`, valores en JSON)."""

    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self._db.commit()

    def get(self, key: str, default=None):
        with self._lock:
            row = self._db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except ValueError:
            return default

    def set(self, key: str, value) -> None:
        with self._lock:
            if value is None:
                self._db.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                self._db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))
            self._db.commit()


class MemorySettings(Settings):
    """Sin base de datos (pruebas)."""

    def __init__(self):  # noqa: D107 (no llama al padre a propósito)
        self._values: dict = {}

    def get(self, key, default=None):
        return self._values.get(key, default)

    def set(self, key, value):
        if value is None:
            self._values.pop(key, None)
        else:
            self._values[key] = value


# ------------------------------------------------------------------ fuentes


class Deezer:
    API = "https://api.deezer.com"

    def __init__(self):
        self._client = httpx.AsyncClient(timeout=15.0, headers=UA)
        self._playlists = dict(countries.DEEZER_PLAYLISTS)

    async def _get(self, path: str, **params) -> dict:
        resp = await self._client.get(f"{self.API}{path}", params=params)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "error" in data:
            raise SourceError((data["error"] or {}).get("message") or "Deezer no respondió bien")
        return data

    async def search(self, query: str, limit: int = 30) -> list[dict]:
        data = await self._get("/search", q=query, limit="50")
        out = []
        for t in data.get("data", []):
            title, artist = (t.get("title") or "").strip(), ((t.get("artist") or {}).get("name") or "").strip()
            if title and artist:
                out.append({"title": title, "artist": artist, "album": (t.get("album") or {}).get("title") or None,
                            "year": None, "listeners": int(t.get("rank") or 0), "url": t.get("link")})
        return clean_search(query, out, limit)

    async def _playlist_id(self, code: str) -> int:
        if code in self._playlists:
            return self._playlists[code]
        name = "Worldwide" if code == "world" else countries.DEEZER_NAMES.get(code, countries.english(code).title())
        data = await self._get("/search/playlist", q=f"Top {name}", limit="25")
        for p in data.get("data", []):
            if (p.get("user") or {}).get("id") == countries.DEEZER_CHARTS_USER and \
                    (p.get("title") or "").casefold() == f"top {name}".casefold():
                self._playlists[code] = int(p["id"])
                return self._playlists[code]
        raise SourceError("Deezer no tiene lista de ese país")

    async def chart(self, code: str | None, limit: int = 100) -> list[dict]:
        playlist = await self._playlist_id(code or "world")
        data = await self._get(f"/playlist/{playlist}/tracks", limit=str(limit))
        return [{"title": t["title"], "artist": (t.get("artist") or {}).get("name", ""),
                 "album": (t.get("album") or {}).get("title")}
                for t in data.get("data", []) if t.get("title")][:limit]

    async def artist_top(self, artist: str, limit: int = 50) -> list[dict]:
        data = await self._get("/search/artist", q=artist, limit="1")
        found = data.get("data") or []
        if not found:
            raise SourceError("Deezer no encontró ese artista")
        top = await self._get(f"/artist/{found[0]['id']}/top", limit=str(limit))
        return [{"title": t["title"], "artist": (t.get("artist") or {}).get("name", ""),
                 "album": (t.get("album") or {}).get("title")} for t in top.get("data", []) if t.get("title")]


class AppleMusic:
    """Feed público de Apple: lo más escuchado en Apple Music en un país. A veces responde 502: se reintenta."""

    FEED = "https://rss.marketingtools.apple.com/api/v2/{cc}/music/most-played/{n}/songs.json"

    def __init__(self):
        self._client = httpx.AsyncClient(timeout=10.0, headers=UA, follow_redirects=True)

    async def chart(self, code: str | None, limit: int = 100) -> list[dict]:
        if not code:
            raise SourceError("Apple Music no tiene lista mundial")
        last: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self._client.get(self.FEED.format(cc=code, n=min(limit, 100)))
                resp.raise_for_status()
                results = resp.json()["feed"]["results"]
                return [{"title": r["name"], "artist": r.get("artistName", ""), "album": None}
                        for r in results if r.get("name")][:limit]
            except (httpx.HTTPError, ValueError, KeyError) as e:
                last = e
                await asyncio.sleep(1 + attempt * 2)
        raise SourceError(f"Apple Music no respondió ({last})")


class ListenBrainz:
    API = "https://api.listenbrainz.org/1"

    def __init__(self):
        self._client = httpx.AsyncClient(timeout=30.0, headers=UA)

    async def chart(self, code: str | None, limit: int = 100) -> list[dict]:
        if code:
            raise SourceError("ListenBrainz solo tiene lista mundial")
        resp = await self._client.get(f"{self.API}/stats/sitewide/recordings", params={"count": limit, "range": "week"})
        resp.raise_for_status()
        recs = (resp.json().get("payload") or {}).get("recordings") or []
        return [{"title": r["track_name"], "artist": r.get("artist_name", ""), "album": r.get("release_name")}
                for r in recs if r.get("track_name")][:limit]


# ------------------------------------------------------------------ todo junto


class Sources:
    def __init__(self, lastfm: LastFM, settings: Settings | None = None, database: str | None = None,
                 deezer: Deezer | None = None, apple: AppleMusic | None = None,
                 listenbrainz: ListenBrainz | None = None):
        self.lastfm = lastfm
        self.settings = settings or MemorySettings()
        self.deezer, self.apple, self.listenbrainz = deezer, apple, listenbrainz
        self._env_key, self._env_secret = lastfm.api_key, lastfm.secret
        self._cache: dict[str, dict] = {}
        self._busy: set[str] = set()
        self._tasks: set = set()
        self._searches: dict[tuple[str, str], tuple[float, list]] = {}
        self._db = None
        self._lock = threading.Lock()
        if database:
            self._db = sqlite3.connect(database, check_same_thread=False)
            with self._lock:
                self._db.execute("""CREATE TABLE IF NOT EXISTS chart_cache (
                    key TEXT PRIMARY KEY, source TEXT NOT NULL, tracks TEXT NOT NULL,
                    fetched_at INTEGER NOT NULL, requested_at INTEGER NOT NULL DEFAULT 0, error TEXT)""")
                self._db.commit()
                for key, source, tracks, fetched, requested, error in self._db.execute(
                        "SELECT key, source, tracks, fetched_at, requested_at, error FROM chart_cache"):
                    self._cache[key] = {"source": source, "tracks": json.loads(tracks), "fetchedAt": fetched,
                                        "requestedAt": requested, "error": error}
        self.apply_lastfm()

    # ---------------------------------------------------------- Last.fm (clave del entorno o de la web)

    def apply_lastfm(self) -> None:
        key = self.settings.get("lastfm_key") or self._env_key
        secret = self.settings.get("lastfm_secret") or self._env_secret
        on = self.settings.get("lastfm_on", True)
        self.lastfm.api_key = key if on else ""
        self.lastfm.secret = secret if on else ""

    @property
    def lastfm_has_key(self) -> bool:
        return bool(self.settings.get("lastfm_key") or self._env_key)

    # ---------------------------------------------------------- qué hay

    def available(self, name: str) -> bool:
        if name == "lastfm":
            return self.lastfm.enabled
        return getattr(self, name, None) is not None

    def search_source(self) -> str | None:
        wanted = self.settings.get("search")
        order = ([wanted] if wanted in SEARCH_ORDER else []) + list(SEARCH_ORDER)
        return next((n for n in order if self.available(n)), None)

    def chart_default(self) -> str:
        wanted = self.settings.get("charts")
        return wanted if wanted in CHART_ORDER else "apple"

    def chart_source(self, key: str) -> str:
        """La fuente elegida para una lista («world» o el país), sin mirar si está disponible."""
        chosen = (self.settings.get("charts_by_list") or {}).get(key)
        return chosen if chosen in CHART_ORDER else self.chart_default()

    @staticmethod
    def _can(name: str, key: str) -> bool:
        if key == "world":
            return name != "apple"
        if name == "listenbrainz":
            return False
        return name != "deezer" or key in countries.DEEZER_PLAYLISTS or key in countries.COUNTRIES

    def _chart_order(self, key: str) -> list[str]:
        first = self.chart_source(key)
        order = [first] + [n for n in CHART_ORDER if n != first]
        return [n for n in order if self.available(n) and self._can(n, key)]

    @property
    def enabled(self) -> bool:
        """Hay búsqueda y listas (para la app: el antiguo «lastfm» de /status)."""
        return self.search_source() is not None or any(self.available(n) for n in CHART_ORDER)

    def status(self) -> dict:
        return {"search": self.search_source(), "charts": self.chart_default(),
                "lists": {k: (self._chart_order(k) or [None])[0] for k in countries.APP_LISTS}}

    # ---------------------------------------------------------- búsqueda

    async def search(self, query: str) -> tuple[list[dict], str]:
        wanted = self.search_source()
        if wanted is None:
            raise ValueError("Este servidor no tiene configurada la búsqueda")
        cache_key = (wanted, " ".join(query.lower().split()))
        hit = self._searches.get(cache_key)
        if hit and time.time() - hit[0] < SEARCH_TTL:
            return hit[1], wanted
        errors = []
        for name in [wanted] + [n for n in SEARCH_ORDER if n != wanted and self.available(n)]:
            try:
                results = await getattr(self, name).search(query)
                if name == wanted:
                    if len(self._searches) >= SEARCH_MAX:  # fuera la más antigua
                        self._searches.pop(min(self._searches, key=lambda k: self._searches[k][0]), None)
                    self._searches[cache_key] = (time.time(), results)
                return results, name
            except (httpx.HTTPError, SourceError, ValueError, KeyError) as e:
                errors.append(f"{NAMES[name]}: {e}")
                log.warning("Búsqueda en %s falló: %s", NAMES[name], e)
        raise ValueError("No se pudo buscar ahora mismo (" + "; ".join(errors) + ")")

    # ---------------------------------------------------------- listas

    def _save(self, key: str, entry: dict) -> None:
        self._cache[key] = entry
        if self._db is None:
            return
        with self._lock:
            self._db.execute(
                "INSERT INTO chart_cache (key, source, tracks, fetched_at, requested_at, error) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET source = excluded.source, tracks = excluded.tracks, "
                "fetched_at = excluded.fetched_at, requested_at = excluded.requested_at, error = excluded.error",
                (key, entry["source"], json.dumps(entry["tracks"]), entry["fetchedAt"], entry["requestedAt"],
                 entry.get("error")))
            self._db.commit()

    async def _download(self, key: str, code: str | None, kind: str | None, value: str | None) -> tuple[list, str]:
        if kind:
            order = ["lastfm"] if kind in ("tag", "user") else ["lastfm", "deezer"]
            order = [n for n in order if self.available(n)]
            if not order:
                raise ValueError("Las listas de etiquetas y de usuarios de Last.fm necesitan Last.fm activado")
        else:
            order = self._chart_order(key)
            if not order:
                raise ValueError("No hay ninguna fuente de listas disponible para esa lista")
        errors = []
        for name in order:
            try:
                if name == "lastfm":
                    country = countries.english(code) if code else None
                    tracks = await self.lastfm.charts(None if kind else country, kind=kind, value=value)
                elif kind:
                    tracks = await self.deezer.artist_top(value or "")  # type: ignore[union-attr]
                else:
                    tracks = await getattr(self, name).chart(code)
                if tracks:
                    return tracks, name
                errors.append(f"{NAMES[name]}: vacía")
            except (httpx.HTTPError, SourceError, ValueError, KeyError) as e:
                errors.append(f"{NAMES[name]}: {e}")
                log.warning("Lista %s en %s falló: %s", key, NAMES[name], e)
        raise ValueError("; ".join(errors))

    async def check(self, kind: str, value: str) -> None:
        """Antes de guardar una lista extra: que exista y tenga canciones (ValueError si no)."""
        await self._download(self.key_for(None, kind, value), None, kind, value)

    @staticmethod
    def key_for(code: str | None, kind: str | None = None, value: str | None = None) -> str:
        return f"x|{kind}|{(value or '').lower()}" if kind else (code or "world")

    async def chart(self, code: str | None, kind: str | None = None, value: str | None = None,
                    force: bool = False, requested: bool = True) -> dict:
        """La lista (de la base de datos si es reciente). Devuelve {tracks, source, updatedAt}.
        Si la guardada está vieja se da igualmente y se renueva por detrás (nadie espera a Apple o Deezer)."""
        key = self.key_for(code, kind, value)
        now = int(time.time())
        cached = self._cache.get(key)
        if cached and cached["tracks"] and not force:
            if requested and now - cached.get("requestedAt", 0) > 3600:
                cached["requestedAt"] = now
                self._save(key, cached)
            expired = now - cached["fetchedAt"] >= self._ttl(key, cached, kind)
            if expired and key not in self._busy and now >= cached.get("retryAt", 0):
                self._busy.add(key)
                task = asyncio.get_running_loop().create_task(self._renew(key, code, kind, value))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            return {"tracks": cached["tracks"], "source": cached["source"], "updatedAt": cached["fetchedAt"]}
        return await self._fetch(key, code, kind, value, requested)

    def _ttl(self, key: str, cached: dict, kind: str | None) -> int:
        # Si salió de otra fuente porque la elegida falló, se vuelve a probar la elegida antes (1 hora).
        if cached.get("stale"):
            return 0
        preferred = self._chart_order(key)[:1]
        return FRESH if kind or not preferred or cached["source"] == preferred[0] else 3600

    async def _renew(self, key: str, code: str | None, kind: str | None, value: str | None) -> None:
        try:
            await self._fetch(key, code, kind, value, requested=False)
        except ValueError as e:
            log.warning("No se pudo renovar la lista %s: %s", key, e)
        finally:
            self._busy.discard(key)

    async def _fetch(self, key: str, code: str | None, kind: str | None, value: str | None, requested: bool) -> dict:
        now = int(time.time())
        cached = self._cache.get(key)
        try:
            tracks, source = await self._download(key, code, kind, value)
        except ValueError as e:
            if cached and cached["tracks"]:
                cached["error"] = str(e)
                cached["stale"] = False
                cached["retryAt"] = now + 600  # no reintentar en cada petición: dentro de 10 minutos
                if requested:
                    cached["requestedAt"] = now
                self._save(key, cached)
                return {"tracks": cached["tracks"], "source": cached["source"], "updatedAt": cached["fetchedAt"]}
            raise
        entry = {"source": source, "tracks": tracks, "fetchedAt": now,
                 "requestedAt": now if requested or not cached else cached.get("requestedAt", 0), "error": None}
        self._save(key, entry)
        return {"tracks": tracks, "source": source, "updatedAt": now}

    def cached(self) -> dict[str, dict]:
        return dict(self._cache)

    def forget(self) -> None:
        """Al cambiar las fuentes: la próxima vez se descargan de nuevo (lo guardado sigue valiendo si fallan)."""
        for entry in self._cache.values():
            entry["stale"] = True

    async def refresh(self, force: bool = False) -> int:
        """Renueva las listas que la gente abre (y las de la app). Devuelve cuántas se descargaron."""
        now = int(time.time())
        keys = set(countries.APP_LISTS)  # las de la app, siempre (así nadie espera a la primera descarga)
        for key, entry in self._cache.items():
            if now - entry.get("requestedAt", 0) < KEEP_REFRESHING:
                keys.add(key)
        done = 0
        for key in sorted(keys):
            entry = self._cache.get(key)
            kind = value = code = None
            if key.startswith("x|"):
                _, kind, value = key.split("|", 2)
            elif key != "world":
                code = key
            if not force and entry and entry["tracks"] and now - entry["fetchedAt"] < self._ttl(key, entry, kind):
                continue
            try:
                await self._fetch(key, code, kind, value, requested=False)
                done += 1
            except ValueError as e:
                log.warning("No se pudo renovar la lista %s: %s", key, e)
        return done

    async def keep_fresh(self) -> None:
        """Tarea de fondo: cada hora renueva las listas que lo necesiten."""
        while True:
            try:
                n = await self.refresh()
                if n:
                    log.info("Listas de éxitos renovadas: %d", n)
            except Exception as e:  # noqa: BLE001 (que la tarea no se pare nunca)
                log.warning("Error renovando listas: %s", e)
            await asyncio.sleep(3600)
