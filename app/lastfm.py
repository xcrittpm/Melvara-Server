"""Búsqueda de canciones en Last.fm con la clave de API de quien despliega el servidor.

Solo se usan título, artista y álbum (no imágenes ni biografías), y se citan como datos de Last.fm.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger("sonora.lastfm")
API = "https://ws.audioscrobbler.com/2.0/"


class LastFM:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._client = httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "SonoraServer/2"})

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def _call(self, method: str, **params) -> dict:
        resp = await self._client.get(API, params={"method": method, "api_key": self.api_key, "format": "json", **params})
        resp.raise_for_status()
        return resp.json()

    async def search(self, query: str, limit: int = 30) -> list[dict]:
        data = await self._call("track.search", track=query, limit=str(limit))
        items = data.get("results", {}).get("trackmatches", {}).get("track", []) or []
        out = []
        for t in items:
            name, artist = (t.get("name") or "").strip(), (t.get("artist") or "").strip()
            if name and artist:
                out.append({"title": name, "artist": artist, "album": None, "year": None,
                            "listeners": int(t.get("listeners") or 0), "url": t.get("url")})
        # El álbum solo viene al pedir cada canción: se pide para las primeras.
        for item in out[:10]:
            try:
                info = await self._call("track.getInfo", track=item["title"], artist=item["artist"])
                album = (info.get("track", {}).get("album") or {}).get("title")
                item["album"] = album or None
            except (httpx.HTTPError, ValueError):
                pass
        return out
