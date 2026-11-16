"""Lista de deseos: canciones que cada persona quiere y no tiene en su Navidrome.

Solo se guardan datos (título, artista, álbum, año), nunca música. Cada persona ve solo su lista.
"""

from __future__ import annotations

import sqlite3
import threading
import time

MAX_ITEMS_PER_USER = 5000
MAX_FIELD = 300

# Estado de cada deseo. Lo pone una aplicación externa con la ruta de servicio:
# pendiente → descargando → en la biblioteca; o no encontrada; o hecho a mano.
STATUSES = ("pending", "downloading", "in_library", "not_found", "done")
_COLS = "id, title, artist, album, year, added_at, status, status_note, status_at"


class Wishlist:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS wishes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user TEXT NOT NULL,
                    title TEXT NOT NULL,
                    artist TEXT NOT NULL,
                    album TEXT,
                    year INTEGER,
                    added_at INTEGER NOT NULL,
                    UNIQUE(user, title, artist)
                )"""
            )
            # Bases anteriores a los estados: se añaden las columnas (todo queda «pending»)
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(wishes)")}
            if "status" not in cols:
                self._db.execute("ALTER TABLE wishes ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")
            if "status_note" not in cols:
                self._db.execute("ALTER TABLE wishes ADD COLUMN status_note TEXT")
            if "status_at" not in cols:
                self._db.execute("ALTER TABLE wishes ADD COLUMN status_at INTEGER")
            self._db.commit()

    @staticmethod
    def _row(r) -> dict:
        return {"id": r[0], "title": r[1], "artist": r[2], "album": r[3], "year": r[4], "addedAt": r[5],
                "status": r[6], "statusNote": r[7], "statusAt": r[8]}

    def list(self, user: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_COLS} FROM wishes WHERE user = ? ORDER BY added_at DESC",
                (user,),
            ).fetchall()
        return [self._row(r) for r in rows]

    def add(self, user: str, title: str, artist: str, album: str | None, year: int | None) -> dict:
        title, artist = title.strip()[:MAX_FIELD], artist.strip()[:MAX_FIELD]
        album = (album or "").strip()[:MAX_FIELD] or None
        if not title or not artist:
            raise ValueError("Faltan el título o el artista")
        if year is not None and not (1000 <= year <= 3000):
            year = None
        with self._lock:
            count = self._db.execute("SELECT COUNT(*) FROM wishes WHERE user = ?", (user,)).fetchone()[0]
            if count >= MAX_ITEMS_PER_USER:
                raise ValueError("La lista está llena")
            self._db.execute(
                """INSERT INTO wishes (user, title, artist, album, year, added_at) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user, title, artist) DO UPDATE SET
                       album = COALESCE(excluded.album, album), year = COALESCE(excluded.year, year)""",
                (user, title, artist, album, year, int(time.time())),
            )
            self._db.commit()
            r = self._db.execute(
                f"SELECT {_COLS} FROM wishes WHERE user = ? AND title = ? AND artist = ?",
                (user, title, artist),
            ).fetchone()
        return self._row(r)

    def all_after(self, after: int, limit: int) -> list[dict]:
        """Deseos de todos los usuarios con id > after, de menor a mayor (para la ruta de servicio)."""
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_COLS}, user FROM wishes WHERE id > ? ORDER BY id LIMIT ?",
                (after, limit),
            ).fetchall()
        return [{**self._row(r), "user": r[9]} for r in rows]

    def set_status(self, item_id: int, status: str, note: str | None) -> dict | None:
        """Cambia el estado de un deseo (ruta de servicio). None si no existe. ValueError si no es válido."""
        if status not in STATUSES:
            raise ValueError(f"status debe ser uno de: {', '.join(STATUSES)}")
        note = (note or "").strip()[:MAX_FIELD] or None
        with self._lock:
            cur = self._db.execute(
                "UPDATE wishes SET status = ?, status_note = ?, status_at = ? WHERE id = ?",
                (status, note, int(time.time()), item_id),
            )
            self._db.commit()
            if cur.rowcount == 0:
                return None
            r = self._db.execute(f"SELECT {_COLS}, user FROM wishes WHERE id = ?", (item_id,)).fetchone()
        return {**self._row(r), "user": r[9]}

    def remove(self, user: str, item_id: int) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM wishes WHERE user = ? AND id = ?", (user, item_id))
            self._db.commit()
        return cur.rowcount > 0
