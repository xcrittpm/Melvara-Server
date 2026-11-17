"""Denuncias de perfiles y playlists compartidas entre personas del mismo Navidrome.

La app no tiene contenido público: solo se ven las personas del mismo servidor. Quien lo administra es quien
puede actuar (hablar con esa persona, quitarle la playlist o borrar la cuenta), así que las denuncias le llegan
a él: las ven las cuentas administradoras de Navidrome en Melvara (Ajustes → Administración → Denuncias).
"""

from __future__ import annotations

import sqlite3
import threading
import time

REASONS = ("offensive", "spam", "harassment", "copyright", "other")
MAX_FIELD = 300
MAX_DETAILS = 1000
MAX_OPEN_PER_USER = 50
_COLS = "id, reporter, reported, reason, details, playlist_id, playlist_name, created_at, resolved_at, resolved_by"


class Reports:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reporter TEXT NOT NULL,
                    reported TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    details TEXT,
                    playlist_id TEXT,
                    playlist_name TEXT,
                    created_at INTEGER NOT NULL,
                    resolved_at INTEGER,
                    resolved_by TEXT
                )"""
            )
            self._db.commit()

    @staticmethod
    def _row(r) -> dict:
        return {"id": r[0], "reporter": r[1], "reported": r[2], "reason": r[3], "details": r[4],
                "playlistId": r[5], "playlistName": r[6], "createdAt": r[7], "resolvedAt": r[8],
                "resolvedBy": r[9]}

    def add(self, reporter: str, reported: str, reason: str, details: str | None = None,
            playlist_id: str | None = None, playlist_name: str | None = None) -> dict:
        reported = reported.strip()[:MAX_FIELD]
        if not reported:
            raise ValueError("Falta a quién se denuncia")
        if reported.lower() == reporter.lower():
            raise ValueError("No puedes denunciarte a ti")
        if reason not in REASONS:
            raise ValueError(f"reason debe ser uno de: {', '.join(REASONS)}")
        details = (details or "").strip()[:MAX_DETAILS] or None
        playlist_id = (playlist_id or "").strip()[:MAX_FIELD] or None
        playlist_name = (playlist_name or "").strip()[:MAX_FIELD] or None
        with self._lock:
            open_count = self._db.execute(
                "SELECT COUNT(*) FROM reports WHERE reporter = ? AND resolved_at IS NULL", (reporter,)
            ).fetchone()[0]
            if open_count >= MAX_OPEN_PER_USER:
                raise ValueError("Tienes demasiadas denuncias sin revisar")
            cur = self._db.execute(
                """INSERT INTO reports (reporter, reported, reason, details, playlist_id, playlist_name, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (reporter, reported, reason, details, playlist_id, playlist_name, int(time.time())),
            )
            self._db.commit()
            r = self._db.execute(f"SELECT {_COLS} FROM reports WHERE id = ?", (cur.lastrowid,)).fetchone()
        return self._row(r)

    def list(self, include_resolved: bool = False) -> list[dict]:
        where = "" if include_resolved else "WHERE resolved_at IS NULL"
        with self._lock:
            rows = self._db.execute(f"SELECT {_COLS} FROM reports {where} ORDER BY id DESC LIMIT 500").fetchall()
        return [self._row(r) for r in rows]

    def resolve(self, item_id: int, by: str) -> dict | None:
        with self._lock:
            self._db.execute(
                "UPDATE reports SET resolved_at = ?, resolved_by = ? WHERE id = ? AND resolved_at IS NULL",
                (int(time.time()), by, item_id),
            )
            self._db.commit()
            r = self._db.execute(f"SELECT {_COLS} FROM reports WHERE id = ?", (item_id,)).fetchone()
        return self._row(r) if r else None
