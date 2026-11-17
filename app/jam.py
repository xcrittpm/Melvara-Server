"""Escuchar en grupo: una persona pone la música (el anfitrión) y las demás, escaneando su QR, añaden
canciones a su cola desde su Melvara.

Solo personas con cuenta en el mismo Navidrome (cada petición se comprueba con él, como todo en Melvara
Server): no hay invitados. Todo vive en memoria (al reiniciar el servidor, las sesiones se acaban) y caduca
sola tras 6 horas sin uso.
"""

from __future__ import annotations

import secrets
import threading
import time

IDLE_SECONDS = 6 * 3600
MAX_QUEUE = 500
MAX_ADDS = 2000
MAX_TRACK_FIELDS = 40


def _clean_track(t) -> dict | None:
    """Una canción tal como la manda la app (los datos de Track), sin nada raro ni enorme."""
    if not isinstance(t, dict) or not str(t.get("id", "")).strip():
        return None
    out = {}
    for k, v in list(t.items())[:MAX_TRACK_FIELDS]:
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[str(k)[:40]] = v[:500] if isinstance(v, str) else v
    return out


class Jams:
    def __init__(self):
        self._lock = threading.Lock()
        self._jams: dict[str, dict] = {}

    def _expire(self) -> None:
        now = time.time()
        for code in [c for c, j in self._jams.items() if now - j["touched"] > IDLE_SECONDS]:
            del self._jams[code]

    def _get(self, code: str, user: str, *, host: bool = False) -> dict:
        self._expire()
        jam = self._jams.get(code)
        if jam is None:
            raise LookupError("Esta sesión ya no existe")
        if host and jam["host"] != user:
            raise PermissionError("Solo quien la ha abierto puede hacer esto")
        if not host and user != jam["host"] and user not in jam["members"]:
            raise PermissionError("No estás en esta sesión")
        jam["touched"] = time.time()
        return jam

    def start(self, host: str) -> dict:
        with self._lock:
            self._expire()
            # Una sesión por anfitrión: la anterior se cierra.
            for code in [c for c, j in self._jams.items() if j["host"] == host]:
                del self._jams[code]
            code = secrets.token_urlsafe(9)
            self._jams[code] = {"code": code, "host": host, "members": {}, "kicked": set(), "adds": [], "seq": 0,
                                "nowPlaying": None, "queue": [], "touched": time.time(), "started": int(time.time())}
            return self._public(self._jams[code], host)

    def join(self, code: str, user: str) -> dict:
        with self._lock:
            self._expire()
            jam = self._jams.get(code)
            if jam is None:
                raise LookupError("Esta sesión ya no existe")
            if user in jam["kicked"]:
                raise PermissionError("Quien la ha abierto te ha quitado de esta sesión")
            if user != jam["host"]:
                jam["members"][user] = int(time.time())
            jam["touched"] = time.time()
            return self._public(jam, user)

    def leave(self, code: str, user: str) -> None:
        with self._lock:
            jam = self._jams.get(code)
            if jam:
                jam["members"].pop(user, None)

    def end(self, code: str, user: str) -> None:
        with self._lock:
            self._get(code, user, host=True)
            del self._jams[code]

    def kick(self, code: str, user: str, who: str) -> dict:
        with self._lock:
            jam = self._get(code, user, host=True)
            jam["members"].pop(who, None)
            jam["kicked"].add(who)
            return self._public(jam, user)

    def add(self, code: str, user: str, tracks: list) -> dict:
        with self._lock:
            jam = self._get(code, user)
            clean = [t for t in (_clean_track(x) for x in tracks[:50]) if t]
            if not clean:
                raise ValueError("No hay canciones que añadir")
            if len(jam["adds"]) + len(clean) > MAX_ADDS:
                raise ValueError("La sesión tiene demasiadas canciones")
            for t in clean:
                jam["seq"] += 1
                jam["adds"].append({"seq": jam["seq"], "by": user, "track": t, "at": int(time.time())})
            if user in jam["members"]:
                jam["members"][user] = int(time.time())
            return {"added": len(clean)}

    def pending(self, code: str, user: str, after: int) -> dict:
        """Para el anfitrión: lo que han añadido desde `after`."""
        with self._lock:
            jam = self._get(code, user, host=True)
            return {"adds": [a for a in jam["adds"] if a["seq"] > after], "seq": jam["seq"]}

    def update(self, code: str, user: str, now_playing, queue: list) -> None:
        """El anfitrión cuenta qué suena y qué viene (para que lo vean los demás)."""
        with self._lock:
            jam = self._get(code, user, host=True)
            jam["nowPlaying"] = _clean_track(now_playing)
            jam["queue"] = [t for t in (_clean_track(x) for x in queue[:MAX_QUEUE]) if t]

    def state(self, code: str, user: str) -> dict:
        with self._lock:
            jam = self._get(code, user)
            if user in jam["members"]:
                jam["members"][user] = int(time.time())
            return self._public(jam, user)

    @staticmethod
    def _public(jam: dict, user: str) -> dict:
        added_by = {a["track"].get("id"): a["by"] for a in jam["adds"]}
        return {"code": jam["code"], "host": jam["host"], "isHost": jam["host"] == user,
                "members": sorted(jam["members"]), "nowPlaying": jam["nowPlaying"],
                "queue": [{**t, "addedBy": added_by.get(t.get("id"))} for t in jam["queue"]],
                "started": jam["started"]}
