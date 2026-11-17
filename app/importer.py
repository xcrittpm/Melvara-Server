"""Importar en Melvara Server: la app manda la lista de canciones de tu biblioteca de otro proveedor (Apple
Music, Spotify, Plex) y el servidor las busca en tu Navidrome, las añade a tu biblioteca de Melvara, crea o
completa las playlists y ordena, aunque cierres la app.

El servidor actúa como tú con el acceso que ya manda la app (usuario, token y sal; nunca la contraseña),
solo en memoria y mientras dura la importación. Del resultado se guardan solo los números, qué canción del
servidor es cada una y cuáles faltan, para que la app lo recoja.

La búsqueda es la misma que hace la app (Melvara/Catalog/Charts.swift, findTrack): si se cambia una, hay
que cambiar la otra.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import threading
import time
import unicodedata
from urllib.parse import urlencode

import httpx

log = logging.getLogger("melvara.import")

LIBRARY_TITLES = ("Melvara · Mi biblioteca", "Sonora · Mi biblioteca")
MAX_SONGS = 20000
CONCURRENCY = 4
STATUSES = ("running", "done", "failed", "interrupted", "cancelled")


# ------------------------------------------------------------------ texto (como TextNormalizer de la app)

_APOSTROPHES = "'’‘`´"


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()


def tokens(s: str) -> list[str]:
    s = fold(s)
    for a in _APOSTROPHES:
        s = s.replace(a, "")
    return [t for t in re.split(r"[^\w]+|_", s) if t]


def clean_title(s: str) -> str:
    t = re.sub(r"\s*[\(\[](feat|ft|with|con)\.?[^\)\]]*[\)\]]", "", s, flags=re.IGNORECASE)
    t = re.sub(r"\s+-\s+.*$", "", t)
    return t.strip()


def norm(s: str) -> str:
    return " ".join(tokens(s))


def _epoch(value) -> int:
    """Fecha ISO de Navidrome («2024-05-01T10:00:00.123Z») → segundos desde 1970; 0 si no hay."""
    if not value:
        return 0
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


# ------------------------------------------------------------------ Navidrome como la persona


class Navidrome:
    def __init__(self, base: str, user: str, token: str, salt: str, transport: httpx.AsyncBaseTransport | None = None):
        self.base = base.rstrip("/")
        self.auth = {"u": user, "t": token, "s": salt, "v": "1.16.1", "c": "MelvaraServer", "f": "json"}
        # 45 s por petición: una búsqueda que tarda más se da por perdida (y se reintenta una vez).
        self.client = httpx.AsyncClient(timeout=45.0, transport=transport)

    async def close(self) -> None:
        await self.client.aclose()

    async def call(self, endpoint: str, params: list[tuple[str, str]] | None = None, post: bool = False) -> dict:
        data = list(self.auth.items()) + (params or [])
        url = f"{self.base}/rest/{endpoint}.view"
        if post:
            # En el cuerpo: con miles de canciones la dirección sería enorme.
            resp = await self.client.post(url, content=urlencode(data),
                                          headers={"Content-Type": "application/x-www-form-urlencoded"})
        else:
            resp = await self.client.get(url, params=data)
        body = resp.json().get("subsonic-response", {})
        if body.get("status") != "ok":
            raise RuntimeError((body.get("error") or {}).get("message") or f"Navidrome: error en {endpoint}")
        return body

    async def search(self, query: str) -> list[dict]:
        body = await self.call("search3", [("query", query), ("artistCount", "0"), ("albumCount", "0"),
                                           ("songCount", "50")])
        return (body.get("searchResult3") or {}).get("song") or []

    async def playlists(self) -> list[dict]:
        body = await self.call("getPlaylists")
        return (body.get("playlists") or {}).get("playlist") or []

    async def playlist_entries(self, playlist_id: str) -> list[dict]:
        body = await self.call("getPlaylist", [("id", playlist_id)])
        return (body.get("playlist") or {}).get("entry") or []

    async def playlist_ids(self, playlist_id: str) -> list[str]:
        return [e["id"] for e in await self.playlist_entries(playlist_id)]

    async def create_playlist(self, name: str, ids: list[str]) -> str | None:
        body = await self.call("createPlaylist", [("name", name)] + [("songId", i) for i in ids], post=True)
        return (body.get("playlist") or {}).get("id")

    async def rewrite_playlist(self, playlist_id: str, ids: list[str]) -> None:
        """La misma playlist con estas canciones en este orden (no cambia su id ni si es pública)."""
        await self.call("createPlaylist", [("playlistId", playlist_id)] + [("songId", i) for i in ids], post=True)

    async def add_to_playlist(self, playlist_id: str, ids: list[str]) -> None:
        for i in range(0, len(ids), 200):
            await self.call("updatePlaylist", [("playlistId", playlist_id)] + [("songIdToAdd", x) for x in ids[i:i + 200]],
                            post=True)


async def find_track(nd: Navidrome, song: dict) -> str | None:
    """El id de la canción en Navidrome (o None). Igual que findTrack en la app."""
    title = clean_title(song.get("title", ""))
    artist = song.get("artist", "")
    main_artist = tokens(re.split(r"[,&]", artist)[0] if artist else "")
    words = " ".join(tokens(title))
    if not words:
        return None

    def same_artist(t: dict) -> bool:
        have = tokens(f"{t.get('artist', '')} {t.get('displayArtist', '')} {t.get('displayAlbumArtist', '')}")
        return all(a in have for a in main_artist)

    def norm_title(t: dict) -> str:
        return " ".join(tokens(clean_title(t.get("title", ""))))

    queries = [f"{words} {' '.join(main_artist)}", words]
    album = song.get("album") or ""
    if album:
        queries.append(f"{' '.join(tokens(clean_title(album)))} {' '.join(main_artist)}")
    candidates: list[dict] = []
    for q in queries:
        candidates += await nd.search(q)
        if any(same_artist(t) and norm_title(t).startswith(words) for t in candidates):
            break
    matching = [t for t in candidates if same_artist(t)]
    exact = [t for t in matching if norm_title(t) == words]
    variants = [t for t in matching if norm_title(t).startswith(words)]
    if album:
        wanted = norm(album)
        for group in (exact, variants):
            for t in group:
                if norm(t.get("album", "")) == wanted:
                    return t["id"]
    best = exact[0] if exact else (variants[0] if variants else None)
    return best["id"] if best else None


def match_library(entries: list[dict], order: list[dict], known: dict[str, str]) -> dict[str, str]:
    """Qué canción de tu biblioteca es cada una del proveedor, sin volver a buscar en Navidrome: con las mismas
    reglas que `find_track` (título limpio, artista principal, mismo álbum si se puede, y si no una variante).
    `known`: las que ya se saben (clave → id). Devuelve clave → id para todas las que encuentra."""
    by_title: dict[str, list[dict]] = {}
    by_first_word: dict[str, list[dict]] = {}
    for e in entries:
        words = tokens(clean_title(e.get("title", "")))
        if words:
            by_title.setdefault(" ".join(words), []).append(e)
            by_first_word.setdefault(words[0], []).append(e)
    library_ids = {e["id"] for e in entries}
    result: dict[str, str] = {}
    for item in order:
        key = item["key"]
        if known.get(key) in library_ids:
            result[key] = known[key]
            continue
        words = tokens(clean_title(item.get("title", "")))
        if not words:
            continue
        wanted = " ".join(words)
        artist = item.get("artist", "")
        main_artist = tokens(re.split(r"[,&]", artist)[0] if artist else "")

        def same_artist(e: dict) -> bool:
            if not main_artist:
                return True
            have = tokens(f"{e.get('artist', '')} {e.get('displayArtist', '')} {e.get('displayAlbumArtist', '')}")
            return all(a in have for a in main_artist)

        exact = [e for e in by_title.get(wanted, []) if same_artist(e)]
        variants = exact or [e for e in by_first_word.get(words[0], [])
                             if same_artist(e) and " ".join(tokens(clean_title(e.get("title", "")))).startswith(wanted)]
        if not variants:
            continue
        album = norm(item.get("album", ""))
        same_album = [e for e in variants if album and norm(e.get("album", "")) == album]
        result[key] = (same_album or variants)[0]["id"]
    return result


# ------------------------------------------------------------------ trabajos


class ImportJobs:
    def __init__(self, path: str, navidrome_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self.navidrome_url = navidrome_url
        self.transport = transport  # pruebas: un Navidrome simulado
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._tasks: dict[int, asyncio.Task] = {}
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS import_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    status TEXT NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0,
                    total INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    result TEXT,
                    error TEXT
                )"""
            )
            # Lo que estaba en marcha cuando se apagó el servidor ya no puede seguir (el acceso solo estaba en memoria).
            self._db.execute("UPDATE import_jobs SET status = 'interrupted' WHERE status = 'running'")
            self._db.commit()

    @staticmethod
    def _row(r) -> dict:
        return {"id": r[0], "user": r[1], "provider": r[2], "status": r[3], "done": r[4], "total": r[5],
                "createdAt": r[6], "updatedAt": r[7], "result": json.loads(r[8]) if r[8] else None, "error": r[9]}

    def get(self, job_id: int, user: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM import_jobs WHERE id = ? AND user = ?", (job_id, user)).fetchone()
        return self._row(r) if r else None

    def latest(self, user: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM import_jobs WHERE user = ? ORDER BY id DESC LIMIT 1", (user,)).fetchone()
        return self._row(r) if r else None

    def _update(self, job_id: int, **fields) -> None:
        fields["updated_at"] = int(time.time())
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE import_jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))
            self._db.commit()

    @staticmethod
    def validate(body: dict) -> dict:
        songs = body.get("songs")
        if not isinstance(songs, list) or not 1 <= len(songs) <= MAX_SONGS:
            raise ValueError(f"songs debe ser una lista de 1 a {MAX_SONGS} canciones")
        clean = []
        for s in songs:
            if not isinstance(s, dict) or not str(s.get("key", "")).strip() or not str(s.get("title", "")).strip():
                raise ValueError("Cada canción necesita key y title")
            clean.append({"key": str(s["key"])[:600], "title": str(s["title"])[:300], "artist": str(s.get("artist", ""))[:300],
                          "album": str(s.get("album") or "")[:300]})
        keys = {s["key"] for s in clean}

        def key_list(value) -> list[str]:
            return [str(k) for k in value if str(k) in keys] if isinstance(value, list) else []

        playlists = []
        for p in body.get("playlists") or []:
            if isinstance(p, dict) and str(p.get("name", "")).strip():
                playlists.append({"name": str(p["name"])[:200], "keys": key_list(p.get("keys"))})
        # El orden de tu biblioteca en el proveedor: claves, o {key, title, album} (así se reconocen por título y
        # álbum las canciones de importaciones antiguas, que no tienen su id apuntado).
        order = []
        for item in (body.get("order") if isinstance(body.get("order"), list) else [])[:MAX_SONGS]:
            if isinstance(item, dict) and str(item.get("key", "")).strip():
                added = item.get("added")
                order.append({"key": str(item["key"])[:600], "title": str(item.get("title", ""))[:300],
                              "artist": str(item.get("artist", ""))[:300], "album": str(item.get("album") or "")[:300],
                              "added": int(added) if isinstance(added, (int, float)) else None})
            elif isinstance(item, str) and item.strip():
                order.append({"key": item[:600], "title": "", "artist": "", "album": "", "added": None})
        dates = body.get("libraryDates") if isinstance(body.get("libraryDates"), dict) else {}
        library_dates = {str(k)[:100]: int(v) for k, v in list(dates.items())[:MAX_SONGS * 2] if isinstance(v, (int, float))}
        previous = body.get("previousMatches") if isinstance(body.get("previousMatches"), dict) else {}
        return {"provider": str(body.get("provider", "otro"))[:40], "songs": clean, "library": key_list(body.get("library")),
                "playlists": playlists, "reorderLibrary": bool(body.get("reorderLibrary")),
                "order": order, "libraryDates": library_dates,
                "previousMatches": {str(k)[:600]: str(v)[:100] for k, v in list(previous.items())[:MAX_SONGS]}}

    def start(self, user: str, access: tuple[str, str, str], body: dict) -> dict:
        data = self.validate(body)
        with self._lock:
            running = self._db.execute("SELECT id FROM import_jobs WHERE user = ? AND status = 'running'", (user,)).fetchone()
            if running:
                raise ValueError("Ya tienes una importación en marcha")
            now = int(time.time())
            cur = self._db.execute(
                "INSERT INTO import_jobs (user, provider, status, total, created_at, updated_at) VALUES (?, ?, 'running', ?, ?, ?)",
                (user, data["provider"], len(data["songs"]), now, now),
            )
            self._db.commit()
            job_id = cur.lastrowid
        self._tasks[job_id] = asyncio.create_task(self._run(job_id, access, data))
        log.info("Importación %d de %s: %d canciones", job_id, user, len(data["songs"]))
        return self.get(job_id, user)  # type: ignore[return-value]

    def cancel(self, job_id: int, user: str) -> dict | None:
        job = self.get(job_id, user)
        if not job:
            return None
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        if job["status"] == "running":
            self._update(job_id, status="cancelled")
        return self.get(job_id, user)

    async def _run(self, job_id: int, access: tuple[str, str, str], data: dict) -> None:
        nd = Navidrome(self.navidrome_url, *access, transport=self.transport)
        try:
            result = await self._import(job_id, nd, data)
            self._update(job_id, status="done", result=json.dumps(result), done=len(data["songs"]))
            log.info("Importación %d terminada: %d de %d", job_id, result["found"], result["total"])
        except asyncio.CancelledError:
            self._update(job_id, status="cancelled")
            raise
        except Exception as e:  # noqa: BLE001 (cualquier fallo se cuenta en la app)
            message = str(e) or type(e).__name__
            log.warning("Importación %d falló: %s", job_id, message)
            self._update(job_id, status="failed", error=message[:300])
        finally:
            await nd.close()
            self._tasks.pop(job_id, None)

    async def _import(self, job_id: int, nd: Navidrome, data: dict) -> dict:
        songs = data["songs"]
        matches: dict[str, str | None] = {}
        library_keys = set(data["library"])
        library_id = await self._library_playlist(nd)
        have = set(await nd.playlist_ids(library_id)) if library_id else set()
        added = 0
        sem = asyncio.Semaphore(CONCURRENCY)
        progress = {"done": 0, "saved": 0.0}

        failures = {"row": 0, "total": 0}

        async def one(song: dict) -> None:
            async with sem:
                # Una canción que falla (p. ej. Navidrome tarda demasiado con un título raro) no para la importación:
                # se reintenta una vez y, si no, cuenta como no encontrada. Solo si fallan muchas seguidas
                # (Navidrome caído), se para.
                for attempt in (1, 2):
                    try:
                        matches[song["key"]] = await find_track(nd, song)
                        failures["row"] = 0
                        break
                    except Exception as e:  # noqa: BLE001
                        if attempt == 2:
                            log.info("Importación %d: «%s» no se pudo buscar (%s)", job_id, song.get("title"), type(e).__name__)
                            matches[song["key"]] = None
                            failures["row"] += 1
                            failures["total"] += 1
                            if failures["row"] >= 20:
                                raise RuntimeError(f"Navidrome no responde ({type(e).__name__})") from e
            # El avance, canción a canción (guardado como mucho una vez por segundo: la app lo mira cada 3 s).
            progress["done"] += 1
            if time.monotonic() - progress["saved"] >= 1:
                progress["saved"] = time.monotonic()
                self._update(job_id, done=progress["done"])

        # Por tandas de 100: lo encontrado va entrando en tu biblioteca mientras se busca.
        for start in range(0, len(songs), 100):
            chunk = songs[start:start + 100]
            await asyncio.gather(*(one(s) for s in chunk))
            new = []
            for s in chunk:
                tid = matches.get(s["key"])
                if tid and s["key"] in library_keys and tid not in have:
                    have.add(tid)
                    new.append(tid)
            if new:
                if library_id is None:
                    library_id = await nd.create_playlist(LIBRARY_TITLES[0], new)
                else:
                    await nd.add_to_playlist(library_id, new)
                added += len(new)
            self._update(job_id, done=progress["done"])

        all_matches = {**data["previousMatches"], **{k: v for k, v in matches.items() if v}}
        reordered = False
        library_matches: dict[str, str] = {}
        # Mismo orden que en el proveedor: lo que ya tenías y no viene de ahí, primero; detrás lo importado.
        # Una sola línea de tiempo: cada canción de tu biblioteca por la fecha en que la añadiste (en el
        # proveedor o en Melvara con el +, la más antigua); si no se sabe, cuándo llegó a tu servidor
        # (`created` de Navidrome). Igual que la app (LibraryDates.sortDate).
        if data["reorderLibrary"] and library_id and data["order"]:
            entries = await nd.playlist_entries(library_id)
            current = [e["id"] for e in entries]
            # Qué canción de tu biblioteca es cada una del proveedor: lo ya sabido y, para el resto, las mismas
            # reglas que la búsqueda (sin volver a buscar en Navidrome).
            resolved = match_library(entries, data["order"], all_matches)
            provider_date: dict[str, int] = {}
            for item in data["order"]:
                tid = resolved.get(item["key"])
                if tid and item.get("added"):
                    provider_date[tid] = min(provider_date.get(tid, item["added"]), item["added"])
            # Se devuelven a la app: la próxima vez ya están apuntadas.
            library_matches = resolved

            def sort_date(entry: dict) -> int:
                known = [d for d in (provider_date.get(entry["id"]), data["libraryDates"].get(entry["id"])) if d and d > 0]
                if known:
                    return min(known)
                return _epoch(entry.get("created"))

            desired = [e["id"] for e in sorted(entries, key=sort_date)]  # sorted es estable: empates, como estaban
            by_provider = sum(1 for e in entries if e["id"] in provider_date)
            by_melvara = sum(1 for e in entries if e["id"] not in provider_date and data["libraryDates"].get(e["id"]))
            log.info("Importación %d: ordena %d canciones (fecha del proveedor %d, de Melvara %d, de llegada al "
                     "servidor %d); orden del proveedor recibido: %d", job_id, len(entries), by_provider, by_melvara,
                     len(entries) - by_provider - by_melvara, len(data["order"]))
            if desired != current:
                await nd.rewrite_playlist(library_id, desired)
                reordered = True

        created = updated = 0
        if data["playlists"]:
            existing = {p.get("name"): p for p in await nd.playlists()}
            for p in data["playlists"]:
                ids, seen = [], set()
                for key in p["keys"]:
                    tid = matches.get(key) or all_matches.get(key)
                    if tid and tid not in seen:
                        seen.add(tid)
                        ids.append(tid)
                if not ids:
                    continue
                if p["name"] in existing:
                    pid = existing[p["name"]]["id"]
                    current = await nd.playlist_ids(pid)
                    desired = ids + [t for t in current if t not in seen]
                    if desired != current:
                        await nd.rewrite_playlist(pid, desired)
                    updated += 1
                elif await nd.create_playlist(p["name"], ids):
                    created += 1

        missing = [s["key"] for s in songs if not matches.get(s["key"])]
        return {"total": len(songs), "found": len(songs) - len(missing), "addedToLibrary": added,
                "playlistsCreated": created, "playlistsUpdated": updated, "reordered": reordered,
                "matches": {**library_matches, **{k: v for k, v in matches.items() if v}}, "missing": missing}

    async def _library_playlist(self, nd: Navidrome) -> str | None:
        mine = [p for p in await nd.playlists() if p.get("name") in LIBRARY_TITLES and p.get("owner") == nd.auth["u"]]
        mine.sort(key=lambda p: LIBRARY_TITLES.index(p["name"]))
        return mine[0]["id"] if mine else None
