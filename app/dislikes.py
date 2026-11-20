"""«No me gusta»: canciones, álbumes y artistas que cada persona no quiere que le salgan (o que le salgan menos)
en ∞, la radio y Descubrimiento. Se guarda aquí (y no como 1 estrella en Navidrome) para no tocar las
valoraciones; así sirve en todos tus dispositivos. Cada persona ve solo lo suyo.
"""

from __future__ import annotations

import sqlite3
import threading
import time

KINDS = ("track", "album", "artist")
MAX_ITEMS = 5000
_COLS = "kind, item_id, title, artist, created_at"


class Dislikes:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS dislikes (
                    user TEXT NOT NULL COLLATE NOCASE,
                    kind TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    artist TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (user, kind, item_id)
                )"""
            )
            self._db.commit()

    @staticmethod
    def _row(r) -> dict:
        return {"kind": r[0], "id": r[1], "title": r[2], "artist": r[3], "createdAt": r[4]}

    @staticmethod
    def clean(item: dict) -> tuple[str, str, str, str]:
        kind = str(item.get("kind", "")).strip().lower()
        item_id = str(item.get("id", "")).strip()
        if kind not in KINDS:
            raise ValueError(f"kind debe ser uno de: {', '.join(KINDS)}")
        if not item_id or len(item_id) > 200:
            raise ValueError("Falta el identificador")
        return kind, item_id, str(item.get("title") or "")[:200], str(item.get("artist") or "")[:200]

    def list(self, user: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(f"SELECT {_COLS} FROM dislikes WHERE user = ? ORDER BY created_at DESC",
                                    (user,)).fetchall()
        return [self._row(r) for r in rows]

    def add(self, user: str, item: dict) -> dict:
        kind, item_id, title, artist = self.clean(item)
        with self._lock:
            count = self._db.execute("SELECT COUNT(*) FROM dislikes WHERE user = ?", (user,)).fetchone()[0]
            if count >= MAX_ITEMS:
                raise ValueError(f"Como mucho {MAX_ITEMS}")
            self._db.execute(
                "INSERT INTO dislikes (user, kind, item_id, title, artist, created_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user, kind, item_id) DO UPDATE SET title = excluded.title, artist = excluded.artist",
                (user, kind, item_id, title, artist, int(time.time())))
            self._db.commit()
            r = self._db.execute(f"SELECT {_COLS} FROM dislikes WHERE user = ? AND kind = ? AND item_id = ?",
                                 (user, kind, item_id)).fetchone()
        return self._row(r)

    def remove(self, user: str, kind: str, item_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM dislikes WHERE user = ? AND kind = ? AND item_id = ?",
                                   (user, kind, item_id))
            self._db.commit()
        return cur.rowcount > 0
