"""Escuchar en grupo: una persona pone la música (el anfitrión) y las demás, escaneando su QR, se unen. La música
suena solo en el móvil del anfitrión; en el de los demás, su propio reproductor enseña lo que suena en el grupo y lo
que hacen (poner una canción ya, a continuación o al final, pausar, pasar, adelantar, saltar a una de la cola,
reordenarla o quitar) se le pide al anfitrión, que lo aplica (2.16). El anfitrión puede no permitir cambiar lo que
suena (`skip`: pasar, poner ya, pausar, saltar, reordenar, quitar) ni adelantar o atrasar (`seek`); por defecto, sí.

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
MAX_COMMANDS = 200
CONTROL_ACTIONS = ("next", "previous", "seek", "play", "pause", "jump", "move", "remove")
ADD_MODES = ("end", "next", "now")


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
                                "nowPlaying": None, "queue": [], "touched": time.time(), "started": int(time.time()),
                                "perms": {"skip": True, "seek": True}, "commands": [],
                                "position": 0.0, "positionAt": time.time(), "playing": False}
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

    def add(self, code: str, user: str, tracks: list, mode: str = "end") -> dict:
        """Añadir a la cola del grupo: al final (todos pueden), a continuación o ya (como cambiar de canción)."""
        with self._lock:
            jam = self._get(code, user)
            mode = mode if mode in ADD_MODES else "end"
            if mode == "now" and not jam["perms"]["skip"] and user != jam["host"]:
                raise PermissionError("Quien la ha abierto no deja cambiar lo que suena")
            clean = [t for t in (_clean_track(x) for x in tracks[:50]) if t]
            if not clean:
                raise ValueError("No hay canciones que añadir")
            if len(jam["adds"]) + len(clean) > MAX_ADDS:
                raise ValueError("La sesión tiene demasiadas canciones")
            for t in clean:
                jam["seq"] += 1
                jam["adds"].append({"seq": jam["seq"], "by": user, "track": t, "at": int(time.time()), "mode": mode})
            if user in jam["members"]:
                jam["members"][user] = int(time.time())
            return {"added": len(clean)}

    def set_perms(self, code: str, user: str, skip, seek) -> dict:
        """El anfitrión decide si los demás pueden cambiar de canción y adelantar o atrasar."""
        with self._lock:
            jam = self._get(code, user, host=True)
            if isinstance(skip, bool):
                jam["perms"]["skip"] = skip
            if isinstance(seek, bool):
                jam["perms"]["seek"] = seek
            return self._public(jam, user)

    def control(self, code: str, user: str, action: str, position=None, track_id=None, to=None) -> dict:
        """Alguien de la sesión pide pasar de canción, volver, ir a un punto, pausar o seguir, saltar a una de la
        cola (`id`), moverla a otro sitio (`id`, `to`) o quitarla (`id`), si el anfitrión lo permite."""
        with self._lock:
            jam = self._get(code, user)
            if action not in CONTROL_ACTIONS:
                raise ValueError("No se puede hacer eso")
            if not jam["perms"]["seek" if action == "seek" else "skip"] and user != jam["host"]:
                raise PermissionError("Quien la ha abierto no lo permite")
            command = {"action": action, "by": user, "at": int(time.time())}
            if action == "seek":
                if isinstance(position, bool) or not isinstance(position, (int, float)) or not 0 <= position < 24 * 3600:
                    raise ValueError("Falta el punto de la canción")
                command["position"] = float(position)
            if action in ("jump", "move", "remove"):
                if not isinstance(track_id, str) or not track_id.strip() or len(track_id) > 200:
                    raise ValueError("Falta la canción")
                command["id"] = track_id
            if action == "move":
                if isinstance(to, bool) or not isinstance(to, int) or not 0 <= to < MAX_QUEUE:
                    raise ValueError("Falta el sitio")
                command["to"] = to
            jam["seq"] += 1
            command["seq"] = jam["seq"]
            jam["commands"] = (jam["commands"] + [command])[-MAX_COMMANDS:]
            if user in jam["members"]:
                jam["members"][user] = int(time.time())
            return {"ok": True}

    def pending(self, code: str, user: str, after: int) -> dict:
        """Para el anfitrión: lo que han añadido y lo que han pedido desde `after`."""
        with self._lock:
            jam = self._get(code, user, host=True)
            return {"adds": [a for a in jam["adds"] if a["seq"] > after],
                    "commands": [c for c in jam["commands"] if c["seq"] > after], "seq": jam["seq"]}

    def update(self, code: str, user: str, now_playing, queue: list, position=None, playing=None) -> None:
        """El anfitrión cuenta qué suena, por dónde va y qué viene (para que lo vean los demás)."""
        with self._lock:
            jam = self._get(code, user, host=True)
            jam["nowPlaying"] = _clean_track(now_playing)
            jam["queue"] = [t for t in (_clean_track(x) for x in queue[:MAX_QUEUE]) if t]
            if isinstance(position, (int, float)) and not isinstance(position, bool) and position >= 0:
                jam["position"] = float(position)
                jam["positionAt"] = time.time()
            if isinstance(playing, bool):
                jam["playing"] = playing

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
                "started": jam["started"], "perms": dict(jam["perms"]),
                # Por dónde va: `position` segundos cuando el reloj del servidor marcaba `positionAt`
                # (`now` es la hora del servidor, para no depender del reloj de cada iPhone).
                "position": jam["position"], "positionAt": jam["positionAt"], "playing": jam["playing"],
                "now": time.time()}
