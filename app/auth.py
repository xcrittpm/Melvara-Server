"""Seguridad: solo responde a quien tenga cuenta en tu Navidrome.

La app manda en cada petición su acceso de Navidrome (usuario, token y sal: md5(contraseña + sal), lo mismo
que usa con Navidrome; la contraseña nunca viaja). El servidor pregunta al propio Navidrome si ese acceso es
válido. Los accesos no se guardan ni se escriben en los registros: solo un resumen (hash) en memoria.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import deque

import httpx

log = logging.getLogger("melvara.auth")


class Auth:
    def __init__(self, navidrome_url: str, ttl: int = 600, negative_ttl: int = 60):
        self.navidrome_url = navidrome_url.rstrip("/")
        self.ttl = ttl
        self.negative_ttl = negative_ttl
        self._cache: dict[str, tuple[float, str | None]] = {}
        self._lock = threading.Lock()
        self._client = httpx.Client(timeout=10.0)

    @staticmethod
    def key(user: str, token: str, salt: str) -> str:
        return hashlib.sha256(f"{user}|{token}|{salt}".encode()).hexdigest()

    def user_for(self, user: str, token: str, salt: str) -> str | None:
        """Nombre de usuario (en minúsculas) si el acceso es válido en Navidrome; si no, None."""
        if not user or not token or not salt or max(len(user), len(token), len(salt)) > 256:
            return None
        k = self.key(user, token, salt)
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(k)
            if hit and hit[0] > now:
                return hit[1]
        result = user.lower() if self._ask_navidrome(user, token, salt) else None
        with self._lock:
            ttl = self.ttl if result else self.negative_ttl
            self._cache[k] = (now + ttl, result)
            if len(self._cache) > 5000:  # limpieza sencilla
                self._cache = {kk: v for kk, v in self._cache.items() if v[0] > now}
        return result

    def _ask_navidrome(self, user: str, token: str, salt: str) -> bool:
        try:
            resp = self._client.get(
                f"{self.navidrome_url}/rest/ping.view",
                params={"u": user, "t": token, "s": salt, "v": "1.16.1", "c": "MelvaraServer", "f": "json"},
            )
            body = resp.json().get("subsonic-response", {})
        except (httpx.HTTPError, ValueError):
            log.warning("No se pudo contactar con Navidrome para validar un acceso")
            return False
        return body.get("status") == "ok"


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
