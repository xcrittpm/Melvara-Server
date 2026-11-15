"""Cliente mínimo de la API de Plex para descargar la biblioteca de música."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Iterator
from urllib.parse import quote

import httpx

from . import __version__

log = logging.getLogger("sonora.plex")


def _encode_query(params: dict[str, str]) -> str:
    # Plex usa filtros como «updatedAt>>=123»: el «>» debe ir tal cual en el nombre del parámetro.
    return "&".join(f"{quote(k, safe='>')}={quote(str(v), safe='')}" for k, v in params.items())


class Plex:
    def __init__(self, url: str, token: str, client_id: str | None = None):
        self.base = url.rstrip("/")
        self.client = httpx.Client(
            timeout=httpx.Timeout(180.0, connect=10.0),
            headers={
                "Accept": "application/json",
                "X-Plex-Token": token,
                "X-Plex-Product": "Sonora Server",
                "X-Plex-Version": __version__,
                "X-Plex-Client-Identifier": client_id or f"sonora-server-{uuid.uuid4()}",
            },
        )

    def get(self, path: str, params: dict[str, str] | None = None) -> dict:
        url = self.base + path
        if params:
            url += ("&" if "?" in url else "?") + _encode_query(params)
        for attempt in range(3):
            try:
                resp = self.client.get(url)
                resp.raise_for_status()
                return resp.json().get("MediaContainer", {}) if resp.content else {}
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))
        return {}

    def machine_id(self) -> str:
        return self.get("/identity").get("machineIdentifier", "")

    def music_sections(self) -> list[int]:
        dirs = self.get("/library/sections").get("Directory", [])
        return [int(d["key"]) for d in dirs if d.get("type") == "artist"]

    def pages(self, path: str, params: dict[str, str], page_size: int, start: int = 0
              ) -> Iterator[tuple[list[dict], float, int]]:
        """Recorre una lista enorme página a página: (elementos, fracción completada, siguiente posición)."""
        seen = start
        while True:
            q = dict(params)
            q["X-Plex-Container-Start"] = str(start)
            q["X-Plex-Container-Size"] = str(page_size)
            began = time.monotonic()
            page = self.get(path, q)
            items = page.get("Metadata", []) or []
            seen += len(items)
            total = page.get("totalSize")
            if total is None:
                total = seen if len(items) < page_size else None
            fraction = min(seen / total, 1.0) if total else 0.5
            log.debug("Página %s (%s elementos) en %.1f s", start, len(items), time.monotonic() - began)
            if items:
                yield items, fraction, start + len(items)
            if not items or (total is not None and seen >= total):
                return
            start += len(items)


# ------------------------------------------------------------------ conversión


def _int(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def track_from(m: dict, section: int) -> dict | None:
    tid = _int(m.get("ratingKey"))
    if tid is None:
        return None
    album_artist = m.get("grandparentTitle") or "Artista desconocido"
    part = ((m.get("Media") or [{}])[0].get("Part") or [{}])[0]
    return {
        "id": tid,
        "title": m.get("title") or "Sin título",
        "artist": m.get("originalTitle") or album_artist,
        "album_artist": album_artist,
        "album_id": _int(m.get("parentRatingKey")),
        "artist_id": _int(m.get("grandparentRatingKey")),
        "thumb": m.get("parentThumb") or m.get("thumb") or m.get("grandparentThumb"),
        "duration": _int(m.get("duration")) or 0,
        "track_no": _int(m.get("index")),
        "disc_no": _int(m.get("parentIndex")),
        "part_key": part.get("key"),
        "added_at": _int(m.get("addedAt")) or 0,
        "section_id": section,
    }


def album_from(m: dict, section: int) -> dict | None:
    aid = _int(m.get("ratingKey"))
    if aid is None:
        return None
    return {
        "id": aid,
        "title": m.get("title") or "Sin título",
        "artist": m.get("parentTitle") or "Artista desconocido",
        "artist_id": _int(m.get("parentRatingKey")),
        "thumb": m.get("thumb"),
        "year": _int(m.get("year")) or _int(m.get("parentYear")),
        "added_at": _int(m.get("addedAt")) or 0,
        "track_count": _int(m.get("leafCount")),
        "section_id": section,
    }


def artist_from(m: dict, section: int) -> dict | None:
    aid = _int(m.get("ratingKey"))
    if aid is None:
        return None
    return {
        "id": aid,
        "name": m.get("title") or "Artista desconocido",
        "thumb": m.get("thumb"),
        "genres": [g["tag"] for g in (m.get("Genre") or []) if g.get("tag")],
        "section_id": section,
    }
