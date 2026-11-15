"""Pruebas del índice, la búsqueda y la seguridad, sin necesidad de un Plex real."""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from starlette.testclient import TestClient  # noqa: E402

from app import main, text  # noqa: E402
from app.auth import RateLimiter  # noqa: E402
from app.catalog import Catalog  # noqa: E402

GOOD = "token-bueno"
LIMITED = "token-solo-seccion-2"


class FakeAuth:
    """Simula a Plex: un token con acceso a todo, otro solo a la sección 2, el resto sin acceso."""

    def sections_for(self, token):
        return {GOOD: frozenset({1, 2}), LIMITED: frozenset({2})}.get(token)


def artist(i, name, section=1, genres=()):
    return {"id": i, "name": name, "thumb": None, "genres": list(genres), "section_id": section}


def album(i, title, artist_name, artist_id, section=1):
    return {"id": i, "title": title, "artist": artist_name, "artist_id": artist_id, "thumb": f"/t/{i}",
            "year": 2020, "added_at": i, "track_count": 1, "section_id": section}


def track(i, title, artist_name, album_id, artist_id, section=1):
    return {"id": i, "title": title, "artist": artist_name, "album_artist": artist_name, "album_id": album_id,
            "artist_id": artist_id, "thumb": None, "duration": 1000, "track_no": 1, "disc_no": 1,
            "part_key": f"/library/parts/{i}/file.flac", "added_at": i, "section_id": section}


@pytest.fixture()
def catalog(tmp_path):
    cat = Catalog(str(tmp_path / "c.sqlite"))
    cat.insert_artists([artist(1, "Duki", genres=["Trap"]), artist(2, "Metallica", genres=["Metal"]),
                        artist(3, "AC/DC", genres=["Rock"]), artist(4, "Secreto", section=2)], fresh=True)
    cat.insert_albums([album(10, "She Don't Give a Fo", "Duki", 1), album(11, "Master of Puppets", "Metallica", 2),
                       album(12, "Back in Black", "AC/DC", 3), album(13, "Oculto", "Secreto", 4, section=2)],
                      fresh=True)
    cat.insert_tracks([track(100, "She Don't Give a Fo", "Duki", 10, 1),
                       track(101, "Master of Puppets", "Metallica", 11, 2),
                       track(102, "Back in Black", "AC/DC", 12, 3),
                       track(103, "Canción oculta", "Secreto", 13, 4, section=2)], fresh=True)
    cat.set_meta("syncedAt", "1700000000")
    return cat


@pytest.fixture()
def client(catalog):
    main.configure(catalog, FakeAuth(), RateLimiter(1000), None)
    with TestClient(main.app) as c:
        yield c


def test_tokens_match_app():
    assert text.tokens("AC/DC – Back in Black") == ["ac", "dc", "back", "in", "black"]
    assert text.tokens("She Don't Give a Fo") == ["she", "dont", "give", "a", "fo"]
    assert text.tokens("Beyoncé") == ["beyonce"]


def test_search_apostrophes_and_accents(catalog):
    r = catalog.search("she dont give", {1, 2})
    assert [t["title"] for t in r["tracks"]] == ["She Don't Give a Fo"]
    assert r["tracks"][0]["album"] == "She Don't Give a Fo"
    assert r["tracks"][0]["thumb"] == "/t/10"  # la portada sale del álbum


def test_search_typos_and_compact(catalog):
    assert catalog.search("metalica", {1, 2})["artists"][0]["name"] == "Metallica"
    assert catalog.search("acdc", {1, 2})["artists"][0]["name"] == "AC/DC"
    assert catalog.search("duki", {1, 2})["top"]["type"] == "artist"


def test_sections_are_respected(catalog):
    assert catalog.search("oculta", {1})["tracks"] == []
    assert len(catalog.search("oculta", {2})["tracks"]) == 1


def test_incremental_update_keeps_index_consistent(catalog):
    catalog.insert_tracks([track(100, "Nuevo título", "Duki", 10, 1)], fresh=False)
    catalog.insert_albums([album(11, "Ride the Lightning", "Metallica", 2)], fresh=False)
    assert catalog.search("she dont give", {1})["tracks"] == []
    assert catalog.search("nuevo titulo", {1})["tracks"][0]["id"] == "100"
    assert catalog.search("ride the lightning", {1})["albums"][0]["id"] == "11"
    w = catalog.writer
    for table in ("tracks_fts", "albums_fts", "artists_fts", "names_tri"):
        w.execute(f"INSERT INTO {table}({table}) VALUES('integrity-check')")


def test_requires_plex_token(client):
    assert client.get("/health").text == "ok"
    assert client.get("/api/v1/search?q=duki").status_code == 401
    assert client.get("/api/v1/search?q=duki", headers={"X-Plex-Token": "malo"}).status_code == 401
    r = client.get("/api/v1/search?q=duki", headers={"X-Plex-Token": GOOD})
    assert r.status_code == 200 and r.json()["top"]["item"]["name"] == "Duki"
    assert r.headers["cache-control"] == "no-store"


def test_limited_user_only_sees_shared_sections(client):
    h = {"Authorization": f"Bearer {LIMITED}"}
    assert client.get("/api/v1/search?q=duki", headers=h).json()["artists"] == []
    assert client.get("/api/v1/albums/10", headers=h).status_code == 404
    assert client.get("/api/v1/albums/13/tracks", headers=h).json()["tracks"][0]["title"] == "Canción oculta"


def test_endpoints(client):
    h = {"X-Plex-Token": GOOD}
    assert client.get("/api/v1/status", headers=h).json()["counts"]["tracks"] == 4
    assert client.get("/api/v1/albums/10/tracks", headers=h).json()["tracks"][0]["id"] == "100"
    assert client.get("/api/v1/artists/2/albums", headers=h).json()["albums"][0]["title"] == "Master of Puppets"
    assert len(client.get("/api/v1/tracks?ids=100,101", headers=h).json()["tracks"]) == 2
    assert client.get("/api/v1/artists/related?genres=metal&exclude=1", headers=h).json()["artists"][0]["id"] == "2"
    assert client.get("/api/v1/albums/newest?limit=2", headers=h).json()["albums"][0]["id"] == "13"
    assert client.get("/api/v1/tracks?ids=abc", headers=h).status_code == 400


def test_rate_limit(catalog):
    main.configure(catalog, FakeAuth(), RateLimiter(3), None)
    with TestClient(main.app) as c:
        codes = [c.get("/api/v1/status", headers={"X-Plex-Token": GOOD}).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_fts5_available():
    sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(a, content='', tokenize='trigram')")
