"""Artistas parecidos para ∞ y la radio, sin depender de Last.fm (de pago desde el 1/12/2026).

Junta dos fuentes gratuitas y sin clave:
- **Deezer**: artistas relacionados (`/artist/{id}/related`), buenos para géneros populares y música latina.
- **ListenBrainz**: artistas que la gente escucha en la misma sesión (necesita el identificador de MusicBrainz,
  que se busca una vez por artista; MusicBrainz pide como mucho una petición por segundo).

Se guardan 30 días (un día si no se encontró nada). Solo se envía el nombre del artista: nunca quién lo pide.
La app cruza los nombres con los artistas de su Navidrome.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
import time
import unicodedata

import httpx

from .sources import UA

KEEP = 30 * 86400
KEEP_EMPTY = 86400
LB_ALGORITHM = "session_based_days_7500_session_300_contribution_5_threshold_10_limit_100_filter_True_skip_30"


def key(name: str) -> str:
    text = unicodedata.normalize("NFKD", name.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def merge(*lists: list[str], limit: int = 40, exclude: str = "") -> list[str]:
    """Una de cada fuente por turnos (las primeras de cada una son las más parecidas), sin repetir."""
    out, seen = [], {key(exclude)}
    for i in range(max((len(x) for x in lists), default=0)):
        for names in lists:
            if i < len(names) and key(names[i]) not in seen:
                seen.add(key(names[i]))
                out.append(names[i])
    return out[:limit]


class SimilarArtists:
    def __init__(self, path: str, client: httpx.AsyncClient | None = None):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._client = client or httpx.AsyncClient(timeout=12.0, headers=UA)
        self._mb_lock = asyncio.Lock()
        self._mb_last = 0.0
        with self._lock:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS similar_artists (
                    name_key TEXT PRIMARY KEY,
                    names TEXT NOT NULL,
                    fetched_at INTEGER NOT NULL
                )"""
            )
            self._db.commit()

    def _cached(self, name: str) -> list[str] | None:
        with self._lock:
            row = self._db.execute("SELECT names, fetched_at FROM similar_artists WHERE name_key = ?",
                                   (key(name),)).fetchone()
        if not row:
            return None
        names = json.loads(row[0])
        return names if time.time() - row[1] < (KEEP if names else KEEP_EMPTY) else None

    def _store(self, name: str, names: list[str]) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO similar_artists VALUES (?, ?, ?)",
                             (key(name), json.dumps(names, ensure_ascii=False), int(time.time())))
            self._db.commit()

    async def get(self, name: str, limit: int = 40) -> list[str]:
        name = name.strip()[:200]
        if not key(name):
            return []
        cached = self._cached(name)
        if cached is not None:
            return cached[:limit]
        deezer, listenbrainz = await asyncio.gather(self.deezer(name), self.listenbrainz(name), return_exceptions=True)
        lists = [x for x in (deezer, listenbrainz) if isinstance(x, list)]
        if not lists:   # las dos fallaron: no se guarda, se reintenta la próxima vez
            return []
        names = merge(*lists, exclude=name)
        self._store(name, names)
        return names[:limit]

    async def deezer(self, name: str) -> list[str]:
        resp = await self._client.get("https://api.deezer.com/search/artist", params={"q": name, "limit": "10"})
        resp.raise_for_status()
        found = [a for a in resp.json().get("data", []) if key(a.get("name", "")) == key(name)]
        if not found:
            return []
        best = max(found, key=lambda a: a.get("nb_fan") or 0)   # el de verdad suele ser el de más seguidores
        resp = await self._client.get(f"https://api.deezer.com/artist/{best['id']}/related", params={"limit": "40"})
        resp.raise_for_status()
        return [a["name"] for a in resp.json().get("data", []) if a.get("name")]

    async def _mbid(self, name: str) -> str | None:
        async with self._mb_lock:   # MusicBrainz: una petición por segundo como mucho
            wait = 1.1 - (time.monotonic() - self._mb_last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._mb_last = time.monotonic()
            resp = await self._client.get("https://musicbrainz.org/ws/2/artist/",
                                          params={"query": f'artist:"{name}"', "fmt": "json", "limit": "5"})
        resp.raise_for_status()
        for a in resp.json().get("artists", []):
            if key(a.get("name", "")) == key(name) and int(a.get("score") or 0) >= 90:
                return a["id"]
        return None

    async def listenbrainz(self, name: str) -> list[str]:
        mbid = await self._mbid(name)
        if not mbid:
            return []
        resp = await self._client.get("https://labs.api.listenbrainz.org/similar-artists/json",
                                      params={"artist_mbids": mbid, "algorithm": LB_ALGORITHM})
        resp.raise_for_status()
        rows = resp.json()
        rows = rows if isinstance(rows, list) else []
        rows.sort(key=lambda r: -(r.get("score") or 0))
        return [r["name"] for r in rows if r.get("name")][:40]
