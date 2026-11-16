"""Pruebas de Sonora Server (sin red: Navidrome y Last.fm se simulan)."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from starlette.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.auth import Auth, RateLimiter  # noqa: E402
from app.lastfm import LastFM  # noqa: E402
from app.wishlist import Wishlist  # noqa: E402


class FakeAuth(Auth):
    def __init__(self):
        super().__init__("http://navidrome.invalid")

    def _ask_navidrome(self, user, token, salt):
        return token == "bueno"


class FakeLastFM(LastFM):
    def __init__(self, key="clave"):
        super().__init__(key)

    async def search(self, query, limit=30):
        return [{"title": "Tan Lines", "artist": "C Marí", "album": "Sempre Cor", "year": None, "listeners": 1, "url": None}]


def make_client(lastfm_key="clave", per_minute=100):
    tmp = tempfile.mkdtemp()
    main.configure(FakeAuth(), RateLimiter(per_minute), Wishlist(os.path.join(tmp, "s.sqlite")), FakeLastFM(lastfm_key))
    return TestClient(main.app)


def headers(user="ana", token="bueno"):
    return {"X-Sonora-User": user, "X-Sonora-Token": token, "X-Sonora-Salt": "abc"}


def test_status_is_public():
    c = make_client()
    r = c.get("/api/v1/status")
    assert r.status_code == 200 and r.json()["service"] == "sonora-api" and r.json()["lastfm"] is True


def test_needs_valid_navidrome_access():
    c = make_client()
    assert c.get("/api/v1/wishlist").status_code == 401
    assert c.get("/api/v1/wishlist", headers=headers(token="malo")).status_code == 401
    assert c.get("/api/v1/wishlist", headers=headers()).status_code == 200


def test_search():
    c = make_client()
    r = c.get("/api/v1/lastfm/search", params={"q": "tan lines"}, headers=headers())
    assert r.status_code == 200 and r.json()["results"][0]["artist"] == "C Marí"
    assert c.get("/api/v1/lastfm/search", params={"q": ""}, headers=headers()).status_code == 400


def test_search_without_key():
    c = make_client(lastfm_key="")
    assert c.get("/api/v1/status").json()["lastfm"] is False
    assert c.get("/api/v1/lastfm/search", params={"q": "x"}, headers=headers()).status_code == 400


def test_wishlist_is_per_user_and_no_duplicates():
    c = make_client()
    song = {"title": "Tan Lines", "artist": "C Marí", "album": "Sempre Cor", "year": 2023}
    assert c.post("/api/v1/wishlist", json=song, headers=headers("ana")).status_code == 200
    assert c.post("/api/v1/wishlist", json=song, headers=headers("ana")).status_code == 200
    items = c.get("/api/v1/wishlist", headers=headers("ana")).json()["items"]
    assert len(items) == 1 and items[0]["year"] == 2023
    assert c.get("/api/v1/wishlist", headers=headers("luis")).json()["items"] == []
    # Luis no puede borrar lo de Ana.
    assert c.delete(f"/api/v1/wishlist/{items[0]['id']}", headers=headers("luis")).status_code == 404
    assert c.delete(f"/api/v1/wishlist/{items[0]['id']}", headers=headers("ana")).status_code == 200
    assert c.get("/api/v1/wishlist", headers=headers("ana")).json()["items"] == []


def test_wishlist_validation():
    c = make_client()
    assert c.post("/api/v1/wishlist", json={"title": "", "artist": "x"}, headers=headers()).status_code == 400
    assert c.post("/api/v1/wishlist", content=b"no es json", headers=headers()).status_code == 400


def test_rate_limit():
    c = make_client(per_minute=3)
    codes = [c.get("/api/v1/wishlist", headers=headers()).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[-1] == 429
