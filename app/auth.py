"""Seguridad: solo responde a quien tenga una cuenta de Plex con acceso a este servidor de Plex.

Cada petición trae el token de Plex del usuario (el mismo que la app usa con el servidor).
Lo comprobamos preguntando al propio servidor de Plex qué bibliotecas puede ver ese token:
- si Plex lo rechaza, no hay acceso;
- si lo acepta, el usuario solo verá resultados de las secciones de música que tiene compartidas.

Los tokens nunca se guardan ni se escriben en los registros: solo un resumen (hash) en memoria.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import deque

import httpx

log = logging.getLogger("sonora.auth")


class Auth:
    def __init__(self, plex_url: str, ttl: int = 600, negative_ttl: int = 60):
        self.plex_url = plex_url.rstrip("/")
        self.ttl = ttl
        self.negative_ttl = negative_ttl
        self._cache: dict[str, tuple[float, frozenset[int] | None]] = {}
        self._lock = threading.Lock()
        self._client = httpx.Client(timeout=10.0)

    @staticmethod
    def key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def sections_for(self, token: str) -> frozenset[int] | None:
        """Secciones de música que puede ver este token, o None si no tiene acceso."""
        if not token or len(token) > 512:
            return None
        k = self.key(token)
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(k)
            if hit and hit[0] > now:
                return hit[1]
        sections = self._ask_plex(token)
        with self._lock:
            ttl = self.ttl if sections is not None else self.negative_ttl
            self._cache[k] = (now + ttl, sections)
            if len(self._cache) > 5000:  # limpieza sencilla
                self._cache = {kk: v for kk, v in self._cache.items() if v[0] > now}
        return sections

    def _ask_plex(self, token: str) -> frozenset[int] | None:
        try:
            resp = self._client.get(
                f"{self.plex_url}/library/sections",
                headers={"Accept": "application/json", "X-Plex-Token": token},
            )
        except httpx.HTTPError:
            log.warning("No se pudo contactar con Plex para validar un acceso")
            return None
        if resp.status_code != 200:
            return None
        dirs = resp.json().get("MediaContainer", {}).get("Directory", []) or []
        return frozenset(int(d["key"]) for d in dirs if d.get("type") == "artist")


class RateLimiter:
    """Límite de peticiones por minuto para cada usuario (o IP si no hay usuario)."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and q[0] < now - 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            if len(self._hits) > 10000:
                self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > now - 60}
            return True
