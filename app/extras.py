"""Listas extra de Last.fm que añade quien administra el servidor, para todas las personas en Melvara.

Además de los Top por país, Last.fm tiene listas por etiqueta («reggaeton», «80s»…), lo más escuchado por un
usuario concreto de Last.fm y lo más escuchado de un artista. Aquí solo se guarda qué listas hay; las canciones
se piden a Last.fm al abrirlas.
"""

from __future__ import annotations

import sqlite3
import threading
import time

KINDS = ("tag", "user", "artist")
MAX_LISTS = 50
MAX_VALUE = 100
MAX_TITLE = 60
_COLS = "id, kind, value, title, created_by, created_at"


class ExtraLists:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS extra_lists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    value TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    UNIQUE(kind, value)
                )"""
            )
            self._db.commit()

    @staticmethod
    def _row(r) -> dict:
        return {"id": r[0], "kind": r[1], "value": r[2], "title": r[3], "createdBy": r[4], "createdAt": r[5]}

    @staticmethod
    def clean(kind: str, value: str, title: str) -> tuple[str, str, str]:
        kind, value, title = kind.strip().lower(), value.strip()[:MAX_VALUE], title.strip()[:MAX_TITLE]
        if kind not in KINDS:
            raise ValueError(f"kind debe ser uno de: {', '.join(KINDS)}")
        if not value:
            raise ValueError("Falta la etiqueta, el usuario o el artista")
        return kind, value, title or value

    def list(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(f"SELECT {_COLS} FROM extra_lists ORDER BY id").fetchall()
        return [self._row(r) for r in rows]

    def get(self, item_id: int) -> dict | None:
        with self._lock:
            r = self._db.execute(f"SELECT {_COLS} FROM extra_lists WHERE id = ?", (item_id,)).fetchone()
        return self._row(r) if r else None

    def add(self, kind: str, value: str, title: str, by: str) -> dict:
        kind, value, title = self.clean(kind, value, title)
        with self._lock:
            if self._db.execute("SELECT COUNT(*) FROM extra_lists").fetchone()[0] >= MAX_LISTS:
                raise ValueError(f"Como mucho {MAX_LISTS} listas")
            try:
                cur = self._db.execute(
                    "INSERT INTO extra_lists (kind, value, title, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                    (kind, value, title, by, int(time.time())),
                )
            except sqlite3.IntegrityError:
                raise ValueError("Esa lista ya existe")
            self._db.commit()
            new_id = cur.lastrowid
        return self.get(new_id)  # type: ignore[return-value]

    def update(self, item_id: int, kind: str, value: str, title: str) -> dict | None:
        kind, value, title = self.clean(kind, value, title)
        with self._lock:
            try:
                cur = self._db.execute("UPDATE extra_lists SET kind = ?, value = ?, title = ? WHERE id = ?",
                                       (kind, value, title, item_id))
            except sqlite3.IntegrityError:
                raise ValueError("Esa lista ya existe")
            self._db.commit()
            if cur.rowcount == 0:
                return None
        return self.get(item_id)

    def remove(self, item_id: int) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM extra_lists WHERE id = ?", (item_id,))
            self._db.commit()
        return cur.rowcount > 0
