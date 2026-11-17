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


# ------------------------------------------------------------------ Navidrome como la persona


class Navidrome:
    def __init__(self, base: str, user: str, token: str, salt: str, transport: httpx.AsyncBaseTransport | None = None):
        self.base = base.rstrip("/")
        self.auth = {"u": user, "t": token, "s": salt, "v": "1.16.1", "c": "MelvaraServer", "f": "json"}
        self.client = httpx.AsyncClient(timeout=60.0, transport=transport)

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

    async def playlist_ids(self, playlist_id: str) -> list[str]:
        body = await self.call("getPlaylist", [("id", playlist_id)])
        return [e["id"] for e in (body.get("playlist") or {}).get("entry") or []]

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
        order = body.get("order") if isinstance(body.get("order"), list) else []
        previous = body.get("previousMatches") if isinstance(body.get("previousMatches"), dict) else {}
        return {"provider": str(body.get("provider", "otro"))[:40], "songs": clean, "library": key_list(body.get("library")),
                "playlists": playlists, "reorderLibrary": bool(body.get("reorderLibrary")),
                "order": [str(k)[:600] for k in order][:MAX_SONGS],
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
            log.warning("Importación %d: %s", job_id, e)
            self._update(job_id, status="failed", error=str(e)[:300])
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

        async def one(song: dict) -> None:
            async with sem:
                matches[song["key"]] = await find_track(nd, song)

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
            self._update(job_id, done=min(start + 100, len(songs)))

        all_matches = {**data["previousMatches"], **{k: v for k, v in matches.items() if v}}
        reordered = False
        # Mismo orden que en el proveedor: lo que ya tenías y no viene de ahí, primero; detrás lo importado.
        if data["reorderLibrary"] and library_id and data["order"]:
            current = await nd.playlist_ids(library_id)
            current_set = set(current)
            seen: set[str] = set()
            imported = []
            for key in data["order"]:
                tid = all_matches.get(key)
                if tid and tid in current_set and tid not in seen:
                    seen.add(tid)
                    imported.append(tid)
            desired = [t for t in current if t not in seen] + imported
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
                "matches": {k: v for k, v in matches.items() if v}, "missing": missing}

    async def _library_playlist(self, nd: Navidrome) -> str | None:
        mine = [p for p in await nd.playlists() if p.get("name") in LIBRARY_TITLES and p.get("owner") == nd.auth["u"]]
        mine.sort(key=lambda p: LIBRARY_TITLES.index(p["name"]))
        return mine[0]["id"] if mine else None
