"""Last.fm con la clave de API (y el secreto) de quien despliega el servidor.

Así nadie tiene que crearse su propia clave: búsqueda, listas de éxitos y escuchas en el perfil de cada uno.
La clave y el secreto nunca salen del servidor. Solo se usan título, artista y álbum (no imágenes ni
biografías).
"""

from __future__ import annotations

import hashlib
import logging

import httpx

log = logging.getLogger("melvara.lastfm")
API = "https://ws.audioscrobbler.com/2.0/"


# Únicos métodos de Last.fm que la app puede pedir firmar (iniciar sesión y escuchas).
SIGNED_METHODS = {"auth.getMobileSession", "track.updateNowPlaying", "track.scrobble", "track.love", "track.unlove"}


class LastFM:
    def __init__(self, api_key: str, secret: str = ""):
        self.api_key = api_key
        self.secret = secret
        self._client = httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "MelvaraServer/2"})

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @property
    def can_sign(self) -> bool:
        return bool(self.api_key and self.secret)

    async def signed(self, method: str, params: dict[str, str]) -> dict:
        """Llamada firmada en nombre de la app (la sesión del usuario va en `sk`). Devuelve lo que diga Last.fm."""
        if method not in SIGNED_METHODS:
            raise ValueError("Método no permitido")
        if not self.can_sign:
            raise ValueError("Este servidor no tiene configurado el secreto de Last.fm")
        all_params = {k: str(v) for k, v in params.items() if k not in ("api_key", "api_sig", "format", "method")}
        all_params.update({"method": method, "api_key": self.api_key})
        base = "".join(k + all_params[k] for k in sorted(all_params)) + self.secret
        all_params["api_sig"] = hashlib.md5(base.encode()).hexdigest()
        all_params["format"] = "json"
        resp = await self._client.post(API, data=all_params)
        try:
            return resp.json()
        except ValueError:
            return {"error": resp.status_code, "message": "Last.fm no respondió bien"}

    async def charts(self, country: str | None, limit: int = 100) -> list[dict]:
        """Lo más escuchado en Last.fm: en el mundo o en un país (nombre en inglés, p. ej. «spain»)."""
        if country:
            data = await self._call("geo.gettoptracks", country=country, limit=str(limit))
        else:
            data = await self._call("chart.gettoptracks", limit=str(limit))
        tracks = data.get("tracks", {}).get("track", []) or []
        return [{"title": t.get("name", ""), "artist": (t.get("artist") or {}).get("name", "")}
                for t in tracks if t.get("name")][:limit]

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
