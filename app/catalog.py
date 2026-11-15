"""El índice en SQLite: mismas tablas y búsqueda que la app, más el filtro por secciones.

Cada usuario solo ve las secciones de música que su cuenta de Plex tiene compartidas.
"""

from __future__ import annotations

import random
import sqlite3
import threading
from typing import Iterable

from . import text

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks(
    id INTEGER PRIMARY KEY, title TEXT, artist TEXT, album_artist TEXT,
    album_id INTEGER, artist_id INTEGER, thumb TEXT, duration INTEGER, track_no INTEGER,
    disc_no INTEGER, part_key TEXT, added_at INTEGER, section_id INTEGER);
CREATE INDEX IF NOT EXISTS tracks_album ON tracks(album_id);
CREATE INDEX IF NOT EXISTS tracks_artist ON tracks(artist_id);
CREATE TABLE IF NOT EXISTS albums(
    id INTEGER PRIMARY KEY, title TEXT, artist TEXT, artist_id INTEGER, thumb TEXT, year INTEGER,
    added_at INTEGER, track_count INTEGER, section_id INTEGER);
CREATE INDEX IF NOT EXISTS albums_artist ON albums(artist_id);
CREATE INDEX IF NOT EXISTS albums_added ON albums(added_at);
CREATE TABLE IF NOT EXISTS artists(
    id INTEGER PRIMARY KEY, name TEXT, thumb TEXT, genres TEXT, name_norm TEXT, section_id INTEGER);
CREATE INDEX IF NOT EXISTS artists_name ON artists(name_norm);
CREATE VIRTUAL TABLE IF NOT EXISTS tracks_fts USING fts5(title, artist, content='',
    tokenize='unicode61 remove_diacritics 2', prefix='2 3');
CREATE VIRTUAL TABLE IF NOT EXISTS albums_fts USING fts5(title, artist, content='',
    tokenize='unicode61 remove_diacritics 2', prefix='2 3');
CREATE VIRTUAL TABLE IF NOT EXISTS artists_fts USING fts5(name, content='',
    tokenize='unicode61 remove_diacritics 2', prefix='2 3');
CREATE VIRTUAL TABLE IF NOT EXISTS names_tri USING fts5(name, content='', tokenize='trigram');
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

TRACK_SELECT = """
SELECT t.id, t.title, t.artist, COALESCE(t.album_artist, t.artist), COALESCE(a.title, ''),
       t.album_id, t.artist_id, COALESCE(t.thumb, a.thumb), t.duration, t.track_no, t.disc_no,
       t.part_key, t.added_at, t.section_id
FROM tracks t LEFT JOIN albums a ON a.id = t.album_id
"""
ALBUM_COLS = "a.id, a.title, a.artist, a.artist_id, a.thumb, a.year, a.added_at, a.track_count, a.section_id"
ARTIST_COLS = "a.id, a.name, a.thumb, a.genres, a.section_id"


def _sid(v) -> str | None:
    return None if v is None else str(v)


def track_json(r) -> dict:
    # Mismos nombres que el modelo `Track` de la app. Los datos de cada usuario
    # (reproducciones, estrellas) no se devuelven: el índice se crea con la cuenta del dueño.
    return {
        "id": str(r[0]), "title": r[1] or "", "artist": r[2] or "", "albumArtist": r[3] or "",
        "album": r[4] or "", "albumID": _sid(r[5]), "artistID": _sid(r[6]), "thumb": r[7],
        "duration": r[8] or 0, "trackNumber": r[9], "discNumber": r[10], "partKey": r[11],
        "addedAt": r[12] or 0, "viewCount": 0,
    }


def album_json(r) -> dict:
    return {
        "id": str(r[0]), "title": r[1] or "", "artist": r[2] or "", "artistID": _sid(r[3]),
        "thumb": r[4], "year": r[5], "addedAt": r[6] or 0, "trackCount": r[7],
    }


def artist_json(r) -> dict:
    genres = [g for g in (r[3] or "").split("|") if g] or None
    return {"id": str(r[0]), "name": r[1] or "", "thumb": r[2], "genres": genres}


def _fts(s: str | None) -> str | None:
    return None if s is None else text.strip_apostrophes(s)


def _fts_artist(artist: str, album_artist: str | None) -> str:
    if not album_artist or album_artist == artist:
        return artist
    return f"{artist} {album_artist}"


def _in_sections(alias: str, sections: Iterable[int]) -> str:
    ids = ",".join(str(int(s)) for s in sections)
    return f"{alias}.section_id IN ({ids})" if ids else "0"


class Catalog:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self.writer = self._connect()
        self.writer.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    @property
    def reader(self) -> sqlite3.Connection:
        """Una conexión de solo lectura por hilo (las peticiones van en paralelo)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            conn.execute("PRAGMA query_only=1")
            self._local.conn = conn
        return conn

    # ---------------------------------------------------------------- metadatos

    def meta(self, key: str) -> str | None:
        row = self.reader.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        with self._write_lock:
            if value is None:
                self.writer.execute("DELETE FROM meta WHERE key=?", (key,))
            else:
                self.writer.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))

    def counts(self) -> dict:
        r = self.reader
        return {
            "tracks": r.execute("SELECT count(*) FROM tracks").fetchone()[0],
            "albums": r.execute("SELECT count(*) FROM albums").fetchone()[0],
            "artists": r.execute("SELECT count(*) FROM artists").fetchone()[0],
        }

    def track_count(self) -> int:
        return self.reader.execute("SELECT count(*) FROM tracks").fetchone()[0]

    def clear_resume_state(self) -> None:
        with self._write_lock:
            self.writer.execute("DELETE FROM meta WHERE key='fullSync' OR key LIKE 'resume-%'")

    def wipe(self) -> None:
        with self._write_lock:
            self.writer.executescript("""
                DELETE FROM tracks; DELETE FROM albums; DELETE FROM artists;
                INSERT INTO tracks_fts(tracks_fts) VALUES('delete-all');
                INSERT INTO albums_fts(albums_fts) VALUES('delete-all');
                INSERT INTO artists_fts(artists_fts) VALUES('delete-all');
                INSERT INTO names_tri(names_tri) VALUES('delete-all');
                DELETE FROM meta;
            """)

    def checkpoint(self) -> None:
        with self._write_lock:
            self.writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # ---------------------------------------------------------------- escritura

    def _transaction(self, body) -> None:
        with self._write_lock:
            w = self.writer
            w.execute("BEGIN IMMEDIATE")
            try:
                body(w)
                w.execute("COMMIT")
            except Exception:
                w.execute("ROLLBACK")
                raise

    def insert_tracks(self, tracks: list[dict], fresh: bool) -> None:
        """`fresh`: la base está vacía, no hace falta quitar entradas antiguas del índice de texto."""
        if not tracks:
            return

        def body(w: sqlite3.Connection):
            for t in tracks:
                tid = t["id"]
                album_artist = None if t["album_artist"] == t["artist"] else t["album_artist"]
                if not fresh:
                    old = w.execute("SELECT title, artist, album_artist FROM tracks WHERE id=?", (tid,)).fetchone()
                    if old:
                        w.execute(
                            "INSERT INTO tracks_fts(tracks_fts,rowid,title,artist) VALUES('delete',?,?,?)",
                            (tid, _fts(old[0]), _fts(_fts_artist(old[1] or "", old[2]))))
                w.execute(
                    """INSERT OR REPLACE INTO tracks(id,title,artist,album_artist,album_id,artist_id,thumb,
                       duration,track_no,disc_no,part_key,added_at,section_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (tid, t["title"], t["artist"], album_artist, t["album_id"], t["artist_id"],
                     t["thumb"] if t["album_id"] is None else None, t["duration"], t["track_no"],
                     t["disc_no"], t["part_key"], t["added_at"], t["section_id"]))
                w.execute("INSERT INTO tracks_fts(rowid,title,artist) VALUES(?,?,?)",
                          (tid, _fts(t["title"]), _fts(_fts_artist(t["artist"], album_artist))))

        self._transaction(body)

    def insert_albums(self, albums: list[dict], fresh: bool) -> None:
        if not albums:
            return

        def body(w: sqlite3.Connection):
            for a in albums:
                aid = a["id"]
                if not fresh:
                    old = w.execute("SELECT title, artist FROM albums WHERE id=?", (aid,)).fetchone()
                    if old:
                        w.execute("INSERT INTO albums_fts(albums_fts,rowid,title,artist) VALUES('delete',?,?,?)",
                                  (aid, _fts(old[0]), _fts(old[1])))
                        w.execute("INSERT INTO names_tri(names_tri,rowid,name) VALUES('delete',?,?)",
                                  (aid * 2 + 1, text.compact(old[0] or "")))
                w.execute(
                    """INSERT OR REPLACE INTO albums(id,title,artist,artist_id,thumb,year,added_at,track_count,section_id)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (aid, a["title"], a["artist"], a["artist_id"], a["thumb"], a["year"], a["added_at"],
                     a["track_count"], a["section_id"]))
                w.execute("INSERT INTO albums_fts(rowid,title,artist) VALUES(?,?,?)",
                          (aid, _fts(a["title"]), _fts(a["artist"])))
                w.execute("INSERT INTO names_tri(rowid,name) VALUES(?,?)", (aid * 2 + 1, text.compact(a["title"])))

        self._transaction(body)

    def insert_artists(self, artists: list[dict], fresh: bool) -> None:
        if not artists:
            return

        def body(w: sqlite3.Connection):
            for a in artists:
                aid = a["id"]
                if not fresh:
                    old = w.execute("SELECT name FROM artists WHERE id=?", (aid,)).fetchone()
                    if old:
                        w.execute("INSERT INTO artists_fts(artists_fts,rowid,name) VALUES('delete',?,?)",
                                  (aid, _fts(old[0])))
                        w.execute("INSERT INTO names_tri(names_tri,rowid,name) VALUES('delete',?,?)",
                                  (aid * 2, text.compact(old[0] or "")))
                genres = "|" + "|".join(text.fold(g) for g in a["genres"]) + "|" if a["genres"] else None
                w.execute(
                    "INSERT OR REPLACE INTO artists(id,name,thumb,genres,name_norm,section_id) VALUES(?,?,?,?,?,?)",
                    (aid, a["name"], a["thumb"], genres, text.norm(a["name"]), a["section_id"]))
                w.execute("INSERT INTO artists_fts(rowid,name) VALUES(?,?)", (aid, _fts(a["name"])))
                w.execute("INSERT INTO names_tri(rowid,name) VALUES(?,?)", (aid * 2, text.compact(a["name"])))

        self._transaction(body)

    # ---------------------------------------------------------------- lectura

    def tracks_by_ids(self, ids: list[int], sections) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(ids), 500):
            chunk = ",".join(str(int(x)) for x in ids[i:i + 500])
            if not chunk:
                continue
            rows = self.reader.execute(
                f"{TRACK_SELECT} WHERE t.id IN ({chunk}) AND {_in_sections('t', sections)}").fetchall()
            out += [track_json(r) for r in rows]
        return out

    def tracks_in_album(self, album_id: int, sections) -> list[dict]:
        rows = self.reader.execute(
            f"{TRACK_SELECT} WHERE t.album_id=? AND {_in_sections('t', sections)} ORDER BY t.disc_no, t.track_no",
            (album_id,)).fetchall()
        return [track_json(r) for r in rows]

    def tracks_by_artist(self, artist_id: int, limit: int, sections) -> list[dict]:
        rows = self.reader.execute(
            f"{TRACK_SELECT} WHERE t.artist_id=? AND {_in_sections('t', sections)} LIMIT ?",
            (artist_id, limit)).fetchall()
        return [track_json(r) for r in rows]

    def album(self, album_id: int, sections) -> dict | None:
        r = self.reader.execute(
            f"SELECT {ALBUM_COLS} FROM albums a WHERE a.id=? AND {_in_sections('a', sections)}",
            (album_id,)).fetchone()
        return album_json(r) if r else None

    def albums_by_artist(self, artist_id: int, sections) -> list[dict]:
        rows = self.reader.execute(
            f"SELECT {ALBUM_COLS} FROM albums a WHERE a.artist_id=? AND {_in_sections('a', sections)} ORDER BY a.year DESC",
            (artist_id,)).fetchall()
        return [album_json(r) for r in rows]

    def newest_albums(self, limit: int, sections) -> list[dict]:
        rows = self.reader.execute(
            f"SELECT {ALBUM_COLS} FROM albums a WHERE {_in_sections('a', sections)} ORDER BY a.added_at DESC LIMIT ?",
            (limit,)).fetchall()
        return [album_json(r) for r in rows]

    def artist(self, artist_id: int, sections) -> dict | None:
        r = self.reader.execute(
            f"SELECT {ARTIST_COLS} FROM artists a WHERE a.id=? AND {_in_sections('a', sections)}",
            (artist_id,)).fetchone()
        return artist_json(r) if r else None

    def random_tracks(self, n: int, sections) -> list[dict]:
        """Canciones al azar sin recorrer toda la tabla (rápido con millones de filas)."""
        max_id = self.reader.execute("SELECT max(id) FROM tracks").fetchone()[0]
        if not max_id:
            return []
        out, seen = [], set()
        for _ in range(n * 3):
            if len(out) >= n:
                break
            r = self.reader.execute(
                f"{TRACK_SELECT} WHERE t.id>=? AND {_in_sections('t', sections)} ORDER BY t.id LIMIT 1",
                (random.randint(0, max_id),)).fetchone()
            if r and r[0] not in seen:
                seen.add(r[0])
                out.append(track_json(r))
        return out

    def random_albums(self, n: int, sections) -> list[dict]:
        max_id = self.reader.execute("SELECT max(id) FROM albums").fetchone()[0]
        if not max_id:
            return []
        out = []
        for _ in range(n * 3):
            if len(out) >= n:
                break
            r = self.reader.execute(
                f"SELECT {ALBUM_COLS} FROM albums a WHERE a.id>=? AND {_in_sections('a', sections)} ORDER BY a.id LIMIT 1",
                (random.randint(0, max_id),)).fetchone()
            if r:
                out.append(album_json(r))
        return out

    def related_artists(self, names: set[str], genres: set[str], exclude: int | None,
                        limit: int, sections) -> list[dict]:
        result, seen = [], {exclude}
        sec = _in_sections("a", sections)
        if names:
            marks = ",".join("?" * len(names))
            for r in self.reader.execute(
                    f"SELECT {ARTIST_COLS} FROM artists a WHERE a.name_norm IN ({marks}) AND {sec}", tuple(names)):
                if r[0] not in seen:
                    seen.add(r[0])
                    result.append(artist_json(r))
        for genre in list(genres)[:4]:
            if len(result) >= limit:
                break
            for r in self.reader.execute(
                    f"SELECT {ARTIST_COLS} FROM artists a WHERE a.genres LIKE ? AND {sec} ORDER BY random() LIMIT ?",
                    (f"%|{genre}|%", limit)):
                if r[0] not in seen:
                    seen.add(r[0])
                    result.append(artist_json(r))
        return result[:limit]

    # ---------------------------------------------------------------- búsqueda

    def search(self, query: str, sections) -> dict:
        toks = text.tokens(query)
        empty = {"top": None, "tracks": [], "albums": [], "artists": []}
        if not toks:
            return empty
        norm_q = " ".join(toks)
        compact_q = "".join(toks)
        # Todas las palabras deben aparecer; la última puede estar a medias.
        match = " ".join(
            f'"{t}"*' if i == len(toks) - 1 and len(t) >= 2 else f'"{t}"' for i, t in enumerate(toks))
        r = self.reader
        try:
            artists = [artist_json(x) for x in r.execute(
                f"""SELECT {ARTIST_COLS} FROM artists_fts f JOIN artists a ON a.id=f.rowid
                    WHERE artists_fts MATCH ? AND {_in_sections('a', sections)} ORDER BY rank LIMIT 30""",
                (match,))]
            albums = [album_json(x) for x in r.execute(
                f"""SELECT {ALBUM_COLS} FROM albums_fts f JOIN albums a ON a.id=f.rowid
                    WHERE albums_fts MATCH ? AND {_in_sections('a', sections)}
                    ORDER BY bm25(albums_fts, 5.0, 1.0) LIMIT 40""",
                (match,))]
            tracks: list[dict] = []
            if len(compact_q) >= 2:
                ids = [x[0] for x in r.execute(
                    "SELECT rowid FROM tracks_fts WHERE tracks_fts MATCH ? ORDER BY bm25(tracks_fts, 8.0, 3.0) LIMIT 300",
                    (match,))]
                order = {tid: i for i, tid in enumerate(ids)}
                rows = self.tracks_by_ids(ids, sections)
                rows.sort(key=lambda t: -_track_score(t, order.get(int(t["id"]), 300), norm_q))
                tracks = rows[:50]
        except sqlite3.OperationalError:
            return empty

        # Erratas: si no hay artistas o álbumes, probamos por parecido (trigramas).
        if len(compact_q) >= 4 and (not artists or not albums):
            fuzzy = self._fuzzy_names(compact_q)
            if not artists:
                artists = [a for a in (self.artist(i // 2, sections) for i in fuzzy if i % 2 == 0) if a][:10]
            if not albums:
                albums = [a for a in (self.album((i - 1) // 2, sections) for i in fuzzy if i % 2 == 1) if a][:15]
            if not tracks and artists:
                tracks = self.tracks_by_artist(int(artists[0]["id"]), 20, sections)

        artists = _promote_exact(artists, norm_q, "name")
        albums = _promote_exact(albums, norm_q, "title")
        return {"top": _top_hit(tracks, albums, artists, norm_q),
                "tracks": tracks, "albums": albums, "artists": artists}

    def _fuzzy_names(self, compact_q: str) -> list[int]:
        grams = {compact_q[i:i + 3] for i in range(len(compact_q) - 2)}
        if not grams:
            return []
        match = " OR ".join(f'"{g}"' for g in grams)
        ids = [x[0] for x in self.reader.execute(
            "SELECT rowid FROM names_tri WHERE names_tri MATCH ? ORDER BY rank LIMIT 60", (match,))]
        scored = []
        for rowid in ids:
            if rowid % 2 == 0:
                row = self.reader.execute("SELECT name FROM artists WHERE id=?", (rowid // 2,)).fetchone()
            else:
                row = self.reader.execute("SELECT title FROM albums WHERE id=?", ((rowid - 1) // 2,)).fetchone()
            if not row:
                continue
            name = text.compact(row[0] or "")
            other = {name[i:i + 3] for i in range(len(name) - 2)}
            if other:
                sim = len(grams & other) / len(grams | other)
                if sim >= 0.45:
                    scored.append((sim, rowid))
        scored.sort(reverse=True)
        return [rowid for _, rowid in scored]


def _track_score(t: dict, rank: int, query: str) -> float:
    s = -rank * 0.05
    title = text.norm(t["title"])
    if title == query:
        s += 6
    elif title.startswith(query):
        s += 2
    return s


def _promote_exact(items: list[dict], query: str, key: str) -> list[dict]:
    exact = [i for i in items if text.norm(i[key]) == query]
    prefix = [i for i in items if text.norm(i[key]) != query and text.norm(i[key]).startswith(query)]
    rest = [i for i in items if not text.norm(i[key]).startswith(query)]
    return exact + prefix + rest


def _top_hit(tracks, albums, artists, query) -> dict | None:
    if artists and text.norm(artists[0]["name"]) == query:
        return {"type": "artist", "item": artists[0]}
    if albums and text.norm(albums[0]["title"]) == query:
        return {"type": "album", "item": albums[0]}
    if tracks and text.norm(tracks[0]["title"]) == query:
        return {"type": "track", "item": tracks[0]}
    if artists and text.norm(artists[0]["name"]).startswith(query):
        return {"type": "artist", "item": artists[0]}
    if tracks:
        return {"type": "track", "item": tracks[0]}
    if albums:
        return {"type": "album", "item": albums[0]}
    if artists:
        return {"type": "artist", "item": artists[0]}
    return None

