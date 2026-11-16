"""Last.fm con la clave de API (y el secreto) de quien despliega el servidor.

Así nadie tiene que crearse su propia clave: búsqueda, listas de éxitos y escuchas en el perfil de cada uno.
La clave y el secreto nunca salen del servidor. Solo se usan título, artista y álbum (no imágenes ni
biografías).
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata

import httpx

log = logging.getLogger("melvara.lastfm")
API = "https://ws.audioscrobbler.com/2.0/"


# Únicos métodos de Last.fm que la app puede pedir firmar (iniciar sesión y escuchas).
SIGNED_METHODS = {"auth.getMobileSession", "track.updateNowPlaying", "track.scrobble", "track.love", "track.unlove"}


# ------------------------------------------------------------------ limpieza de la búsqueda
#
# track.search de Last.fm busca en el título y también en el artista, así que «the cure olivia rodrigo»
# devuelve todas las canciones de Olivia Rodrigo, y mezcla vídeos, karaokes y subidas de cuentas raras
# con pocos oyentes. Se limpia antes de dárselo a la app.

# Si aparecen en el título (y no en lo buscado), no es la canción sino otra cosa.
JUNK = ("video", "karaoke", "in the style of", "originally performed", "made famous", "tribute",
        "8 bit", "nightcore", "sped up", "slowed", "instrumental version", "backing track", "lyrics")
# «Canción - Remastered 2011», «Canción (2010 Remaster)»… es la misma canción.
VERSION = re.compile(r"\s*(?:-\s*|[(\[])\s*(?:\d{4}\s+)?(?:remaster(?:ed)?|remasterizad[oa])(?:\s+\d{4})?(?:\s+version)?\s*[)\]]?\s*$",
                     re.IGNORECASE)


def _words(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.findall(r"[a-z0-9]+", text)


def clean_search(query: str, items: list[dict], limit: int = 30) -> list[dict]:
    """Quita lo que no es (otras canciones del artista, vídeos, karaokes, subidas raras), junta las
    versiones remasterizadas y pone primero la canción que se llama como lo buscado."""
    wanted = _words(query)
    if not wanted:
        return items[:limit]
    wanted_text = " ".join(wanted)

    def junk(item: dict) -> bool:
        title = " ".join(_words(item["title"]))
        if any(marker in title and marker not in wanted_text for marker in JUNK):
            return True
        # Subidas de cuentas que no son artistas («the cure • Olivia Rodrigo» de «Anúncios limitados…»).
        return "•" in item["title"] or "•" in item["artist"]

    candidates = [i for i in items if not junk(i)]
    top = max((i["listeners"] for i in candidates), default=0)
    candidates = [i for i in candidates if i["listeners"] >= 1000 or i["listeners"] >= top * 0.002]

    # Todas las palabras buscadas tienen que estar en el título o en el artista.
    def relevant(item: dict) -> bool:
        have = set(_words(item["title"])) | set(_words(item["artist"]))
        return all(w in have for w in wanted)

    strict = [i for i in candidates if relevant(i)]
    chosen = strict or candidates

    # Una sola por canción: la versión con más oyentes.
    best: dict[tuple[str, str], dict] = {}
    for item in chosen:
        key = (" ".join(_words(VERSION.sub("", item["title"]))), " ".join(_words(item["artist"])))
        if key not in best or item["listeners"] > best[key]["listeners"]:
            best[key] = item

    def exact(item: dict) -> bool:
        # El título es justo lo buscado (con o sin el nombre del artista).
        title = _words(VERSION.sub("", item["title"]))
        rest = [w for w in wanted if w not in set(_words(item["artist"]))]
        return title == wanted or (bool(rest) and title == rest)

    ordered = sorted(best.values(), key=lambda i: (not exact(i), -i["listeners"]))
    return ordered[:limit]


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
        # Se piden más de las que se dan: la limpieza quita bastantes.
        data = await self._call("track.search", track=query, limit=str(max(limit, 50)))
        items = data.get("results", {}).get("trackmatches", {}).get("track", []) or []
        out = []
        for t in items:
            name, artist = (t.get("name") or "").strip(), (t.get("artist") or "").strip()
            if name and artist:
                out.append({"title": name, "artist": artist, "album": None, "year": None,
                            "listeners": int(t.get("listeners") or 0), "url": t.get("url")})
        out = clean_search(query, out, limit)
        # El álbum solo viene al pedir cada canción: se pide para las primeras.
        for item in out[:10]:
            try:
                info = await self._call("track.getInfo", track=item["title"], artist=item["artist"])
                album = (info.get("track", {}).get("album") or {}).get("title")
                item["album"] = album or None
            except (httpx.HTTPError, ValueError):
                pass
        return out
