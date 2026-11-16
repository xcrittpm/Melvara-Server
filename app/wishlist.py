"""Lista de deseos: canciones que cada persona quiere y no tiene en su Navidrome.

Solo se guardan datos (título, artista, álbum, año), nunca música. Cada persona ve solo su lista.
"""

from __future__ import annotations

import sqlite3
import threading
import time

MAX_ITEMS_PER_USER = 5000
MAX_FIELD = 300


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
            self._db.commit()

    @staticmethod
    def _row(r) -> dict:
        return {"id": r[0], "title": r[1], "artist": r[2], "album": r[3], "year": r[4], "addedAt": r[5]}

    def list(self, user: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, title, artist, album, year, added_at FROM wishes WHERE user = ? ORDER BY added_at DESC",
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
                "SELECT id, title, artist, album, year, added_at FROM wishes WHERE user = ? AND title = ? AND artist = ?",
                (user, title, artist),
            ).fetchone()
        return self._row(r)

    def all_after(self, after: int, limit: int) -> list[dict]:
        """Deseos de todos los usuarios con id > after, de menor a mayor (para la ruta de servicio)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, user, title, artist, album, year, added_at FROM wishes WHERE id > ? ORDER BY id LIMIT ?",
                (after, limit),
            ).fetchall()
        return [{"id": r[0], "user": r[1], "title": r[2], "artist": r[3], "album": r[4], "year": r[5], "addedAt": r[6]}
                for r in rows]

    def remove(self, user: str, item_id: int) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM wishes WHERE user = ? AND id = ?", (user, item_id))
            self._db.commit()
        return cur.rowcount > 0
