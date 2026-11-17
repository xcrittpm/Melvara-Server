"""Social: quién usa Melvara en este servidor y qué escuchan tus seguidores mutuos.

- Personas: Navidrome no da la lista de usuarios a quien no administra, así que Melvara Server apunta a quien
  usa Melvara con él (solo el nombre de usuario y cuándo se conectó). Quien oculta su perfil no sale.
- Lo que escuchas: si lo compartes, la app cuenta aquí cada canción que empieza (se guardan la actual y las
  5 últimas). Solo lo ve quien te sigue y a quien sigues (seguidores mutuos, aceptados si el perfil es
  privado, sin bloqueos) y que también comparte lo suyo. Es solo de Melvara: Navidrome sigue recibiendo lo
  que suena como siempre (y otras apps lo ven como siempre).

Los seguimientos se leen de los perfiles de Melvara en Navidrome (playlists «Melvara · Perfil», públicas),
con el acceso de quien pregunta: así el servidor comprueba lo mismo que ve la app y no se puede engañar.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

RECENT = 5
SEEN_EVERY = 3600          # como mucho se apunta una visita por hora y persona
PROFILE_TTL = 60           # los perfiles leídos de Navidrome se reutilizan un minuto
PROFILE_TITLES = ("Melvara · Perfil", "Sonora · Perfil")
MAX_TRACK_FIELDS = 40


def _clean_track(t) -> dict | None:
    if not isinstance(t, dict) or not str(t.get("id", "")).strip():
        return None
    out = {}
    for k, v in list(t.items())[:MAX_TRACK_FIELDS]:
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[str(k)[:40]] = v[:500] if isinstance(v, str) else v
    return out


def _lower(names) -> set[str]:
    return {str(n).lower() for n in (names or []) if isinstance(n, str)}


def profiles_from_playlists(playlists: list[dict]) -> dict[str, dict]:
    """Perfil de Melvara de cada persona (en minúsculas), sacado de sus playlists de perfil."""
    out: dict[str, dict] = {}
    for p in playlists:
        if p.get("name") not in PROFILE_TITLES:
            continue
        owner = str(p.get("owner", "")).lower()
        try:
            profile = json.loads(p.get("comment") or "{}")
        except ValueError:
            profile = {}
        if not isinstance(profile, dict):
            profile = {}
        # El nombre nuevo manda sobre el viejo.
        if owner not in out or p.get("name") == PROFILE_TITLES[0]:
            out[owner] = profile
    return out


def mutual(a: str, b: str, profiles: dict[str, dict]) -> bool:
    """¿a y b se siguen (aceptados si el perfil es privado), sin bloqueos ni perfiles ocultos?"""
    pa, pb = profiles.get(a.lower()), profiles.get(b.lower())
    if pa is None or pb is None or pa.get("hidden") or pb.get("hidden"):
        return False
    a, b = a.lower(), b.lower()
    if b in _lower(pa.get("blocked")) or a in _lower(pb.get("blocked")):
        return False

    def follows(x: str, px: dict, y: str, py: dict) -> bool:
        return y in _lower(px.get("following")) and (not py.get("isPrivate") or x in _lower(py.get("accepted")))

    return follows(a, pa, b, pb) and follows(b, pb, a, pa)


class Social:
    def __init__(self, path: str, navidrome_url: str = "", transport=None):
        self.navidrome_url = navidrome_url
        self.transport = transport
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}
        self._profiles: dict[str, tuple[float, dict]] = {}
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS social_users (
                    username TEXT PRIMARY KEY COLLATE NOCASE,
                    last_seen INTEGER NOT NULL,
                    hidden INTEGER NOT NULL DEFAULT 0,
                    share INTEGER NOT NULL DEFAULT 0,
                    now_playing TEXT,
                    now_at REAL,
                    recent TEXT NOT NULL DEFAULT '[]'
                )"""
            )
            self._db.commit()

    # ------------------------------------------------------------ personas

    def seen(self, username: str) -> None:
        """Alguien ha usado Melvara con este servidor (se llama en cada petición; se guarda como mucho cada hora)."""
        key = username.lower()
        now = time.time()
        if now - self._seen.get(key, 0) < SEEN_EVERY:
            return
        self._seen[key] = now
        with self._lock:
            self._db.execute(
                "INSERT INTO social_users (username, last_seen) VALUES (?, ?) "
                "ON CONFLICT(username) DO UPDATE SET last_seen = excluded.last_seen",
                (username, int(now)))
            self._db.commit()

    def set_hidden(self, username: str, hidden: bool) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO social_users (username, last_seen, hidden) VALUES (?, ?, ?) "
                "ON CONFLICT(username) DO UPDATE SET hidden = excluded.hidden",
                (username, int(time.time()), int(hidden)))
            if hidden:
                # Oculto: tampoco se comparte lo que escuchas.
                self._db.execute("UPDATE social_users SET share = 0, now_playing = NULL, now_at = NULL, recent = '[]' "
                                 "WHERE username = ?", (username,))
            self._db.commit()

    def people(self, username: str) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT username FROM social_users WHERE hidden = 0 AND username != ? COLLATE NOCASE "
                "ORDER BY username COLLATE NOCASE", (username,)).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------ lo que escuchas

    def set_share(self, username: str, share: bool) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO social_users (username, last_seen, share) VALUES (?, ?, ?) "
                "ON CONFLICT(username) DO UPDATE SET share = excluded.share",
                (username, int(time.time()), int(share)))
            if not share:
                # Dejas de compartir: se borra lo que había.
                self._db.execute("UPDATE social_users SET now_playing = NULL, now_at = NULL, recent = '[]' "
                                 "WHERE username = ?", (username,))
            self._db.commit()

    def played(self, username: str, track) -> None:
        """Empieza una canción. Solo se guarda si compartes lo que escuchas."""
        clean = _clean_track(track)
        if clean is None:
            raise ValueError("Falta la canción")
        with self._lock:
            row = self._db.execute("SELECT share, recent FROM social_users WHERE username = ?", (username,)).fetchone()
            if not row or not row[0]:
                return
            recent = [t for t in json.loads(row[1] or "[]") if t.get("id") != clean["id"]]
            recent = ([clean] + recent)[:RECENT]
            self._db.execute("UPDATE social_users SET now_playing = ?, now_at = ?, recent = ? WHERE username = ?",
                             (json.dumps(clean), time.time(), json.dumps(recent), username))
            self._db.commit()

    def stopped(self, username: str) -> None:
        with self._lock:
            self._db.execute("UPDATE social_users SET now_playing = NULL, now_at = NULL WHERE username = ?",
                             (username,))
            self._db.commit()

    def sharing(self, username: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT share, hidden FROM social_users WHERE username = ?", (username,)).fetchone()
        return bool(row and row[0] and not row[1])

    def cached_profiles(self, username: str) -> dict | None:
        hit = self._profiles.get(username.lower())
        return hit[1] if hit and time.time() - hit[0] < PROFILE_TTL else None

    def remember_profiles(self, username: str, profiles: dict) -> None:
        self._profiles[username.lower()] = (time.time(), profiles)

    async def profiles(self, username: str, access: tuple[str, str, str]) -> dict[str, dict]:
        """Los perfiles de Melvara que ve esta persona en Navidrome (un minuto en memoria)."""
        cached = self.cached_profiles(username)
        if cached is not None:
            return cached
        from .importer import Navidrome
        nd = Navidrome(self.navidrome_url, *access, transport=self.transport)
        try:
            profiles = profiles_from_playlists(await nd.playlists())
        finally:
            await nd.close()
        self.remember_profiles(username, profiles)
        return profiles

    def friends_listening(self, username: str, profiles: dict[str, dict]) -> dict:
        """Lo que escuchan tus seguidores mutuos que comparten (si tú también compartes)."""
        if not self.sharing(username):
            return {"sharing": False, "friends": []}
        with self._lock:
            rows = self._db.execute(
                "SELECT username, now_playing, now_at, recent FROM social_users "
                "WHERE share = 1 AND hidden = 0 AND username != ? COLLATE NOCASE", (username,)).fetchall()
        now = time.time()
        friends = []
        for name, now_json, now_at, recent_json in rows:
            if not mutual(username, name, profiles):
                continue
            playing = json.loads(now_json) if now_json else None
            # «Escuchando ahora» mientras dura la canción (y un par de minutos de margen por pausas).
            if playing and now_at:
                seconds = (playing.get("duration") or 0) / 1000
                if now - now_at > max(seconds, 60) + 120:
                    playing = None
            recent = json.loads(recent_json or "[]")
            if not playing and not recent:
                continue
            friends.append({"user": name, "nowPlaying": playing, "since": int(now_at) if now_at else None,
                            "recent": recent})
        friends.sort(key=lambda f: (f["nowPlaying"] is None, -(f["since"] or 0)))
        return {"sharing": True, "friends": friends}
