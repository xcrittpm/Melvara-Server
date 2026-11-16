"""Pruebas de Melvara Server (sin red: Navidrome y Last.fm se simulan)."""

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
        super().__init__(key, "secreto" if key else "")
        self.sent = None

    async def charts(self, country, limit=100):
        return [{"title": "Tusa", "artist": "KAROL G", "country": country}]

    async def search(self, query, limit=30):
        return [{"title": "Tan Lines", "artist": "C Marí", "album": "Sempre Cor", "year": None, "listeners": 1, "url": None}]


SERVICE = "s" * 40


def make_client(lastfm_key="clave", per_minute=100, service_token=""):
    tmp = tempfile.mkdtemp()
    main.configure(FakeAuth(), RateLimiter(per_minute), Wishlist(os.path.join(tmp, "s.sqlite")), FakeLastFM(lastfm_key),
                   service_token)
    return TestClient(main.app)


def headers(user="ana", token="bueno"):
    return {"X-Melvara-User": user, "X-Melvara-Token": token, "X-Melvara-Salt": "abc"}


def test_status_is_public():
    c = make_client()
    r = c.get("/api/v1/status")
    assert r.status_code == 200 and r.json()["service"] == "melvara-api" and r.json()["lastfm"] is True


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


def test_charts():
    c = make_client()
    r = c.get("/api/v1/lastfm/charts", params={"country": "Spain"}, headers=headers())
    assert r.status_code == 200 and r.json()["tracks"][0]["country"] == "spain"
    assert c.get("/api/v1/lastfm/charts", headers=headers()).json()["tracks"][0]["country"] is None


def test_signed_call_only_allowed_methods_and_signature():
    import asyncio
    lf = LastFM("clave", "secreto")
    try:
        asyncio.run(lf.signed("user.getInfo", {}))
        assert False, "debería rechazarse"
    except ValueError:
        pass
    # La firma: parámetros ordenados (sin format) + secreto, en md5.
    import hashlib
    sent = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"ok": True}

    async def fake_post(url, data):
        sent.update(data)
        return Resp()

    lf._client.post = fake_post
    assert asyncio.run(lf.signed("track.scrobble", {"sk": "s", "artist[0]": "A", "format": "x"})) == {"ok": True}
    base = "".join(k + sent[k] for k in sorted(k for k in sent if k not in ("api_sig", "format"))) + "secreto"
    assert sent["api_sig"] == hashlib.md5(base.encode()).hexdigest() and sent["format"] == "json"
    assert c_status_has_auth()


def c_status_has_auth():
    c = make_client()
    return c.get("/api/v1/status").json()["lastfmAuth"] is True


def test_service_route_off_without_token():
    c = make_client()
    assert c.get("/api/v1/status").json()["serviceApi"] is False
    assert c.get("/api/v1/service/wishlist", headers={"Authorization": f"Bearer {SERVICE}"}).status_code == 404


def test_service_route_reads_everyone_paged():
    c = make_client(service_token=SERVICE)
    assert c.get("/api/v1/status").json()["serviceApi"] is True
    for user, title in [("ana", "Uno"), ("luis", "Dos"), ("ana", "Tres")]:
        c.post("/api/v1/wishlist", json={"title": title, "artist": "X"}, headers=headers(user))
    auth = {"Authorization": f"Bearer {SERVICE}"}
    assert c.get("/api/v1/service/wishlist").status_code == 401
    r = c.get("/api/v1/service/wishlist", headers={"Authorization": "Bearer malo"})
    assert r.status_code == 401 and "error" in r.json()
    page = c.get("/api/v1/service/wishlist", params={"limit": 2}, headers=auth).json()
    assert [i["title"] for i in page["items"]] == ["Uno", "Dos"] and page["items"][1]["user"] == "luis"
    assert page["items"][0]["album"] is None and page["next"] == page["items"][1]["id"]
    rest = c.get("/api/v1/service/wishlist", params={"after": page["next"]}, headers=auth).json()
    assert [i["title"] for i in rest["items"]] == ["Tres"]
    empty = c.get("/api/v1/service/wishlist", params={"after": rest["next"]}, headers=auth).json()
    assert empty == {"items": [], "next": None}
    for bad in [{"after": -1}, {"limit": 0}, {"limit": 501}, {"after": "x"}]:
        assert c.get("/api/v1/service/wishlist", params=bad, headers=auth).status_code == 400
    r = c.get("/api/v1/service/wishlist", headers=auth)
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
    # Solo lectura.
    assert c.post("/api/v1/service/wishlist", headers=auth).status_code == 405


def test_wishlist_batch():
    c = make_client()
    songs = [{"title": "Tusa", "artist": "KAROL G"}, {"title": "90%", "artist": "C Marí", "album": "Sempre Cor (Deluxe)"},
             {"title": "", "artist": "Nadie"}, "no es un objeto", {"title": "Tusa", "artist": "KAROL G", "year": 2019}]
    r = c.post("/api/v1/wishlist", headers=headers(), json={"items": songs})
    assert r.status_code == 200
    assert r.json()["skipped"] == 2 and len(r.json()["items"]) == 3
    listed = c.get("/api/v1/wishlist", headers=headers()).json()["items"]
    assert len(listed) == 2 and any(i["year"] == 2019 for i in listed)
    assert c.post("/api/v1/wishlist", headers=headers(), json={"items": []}).status_code == 400
    assert c.post("/api/v1/wishlist", headers=headers(), json={"items": [{}] * 501}).status_code == 400


def test_service_sets_status_and_the_app_sees_it():
    c = make_client(service_token=SERVICE)
    auth = {"Authorization": f"Bearer {SERVICE}"}
    wid = c.post("/api/v1/wishlist", json={"title": "Uno", "artist": "X"}, headers=headers("ana")).json()["item"]["id"]
    mine = c.get("/api/v1/wishlist", headers=headers("ana")).json()["items"][0]
    assert mine["status"] == "pending" and mine["statusNote"] is None and mine["statusAt"] is None
    url = f"/api/v1/service/wishlist/{wid}"
    # Pide el token, como la lectura
    assert c.patch(url, json={"status": "in_library"}).status_code == 401
    assert c.patch(url, json={"status": "in_library"}, headers={"Authorization": "Bearer malo"}).status_code == 401
    # Valores no válidos
    for bad in [{}, {"status": "cosa"}, {"status": 3}, []]:
        assert c.patch(url, json=bad, headers=auth).status_code == 400
    assert c.patch("/api/v1/service/wishlist/9999", json={"status": "done"}, headers=auth).status_code == 404
    r = c.patch(url, json={"status": "in_library", "note": "En la biblioteca desde el 16/11/2026"}, headers=auth)
    assert r.status_code == 200
    item = r.json()["item"]
    assert item["status"] == "in_library" and item["user"] == "ana" and item["statusAt"] > 0
    mine = c.get("/api/v1/wishlist", headers=headers("ana")).json()["items"][0]
    assert mine["status"] == "in_library" and mine["statusNote"].startswith("En la biblioteca")
    listed = c.get("/api/v1/service/wishlist", headers=auth).json()["items"][0]
    assert listed["status"] == "in_library"
    # Volver a añadir la misma canción no le quita el estado
    c.post("/api/v1/wishlist", json={"title": "Uno", "artist": "X", "album": "Disco"}, headers=headers("ana"))
    assert c.get("/api/v1/wishlist", headers=headers("ana")).json()["items"][0]["status"] == "in_library"
    # Sin token configurado, la ruta no existe
    c2 = make_client()
    assert c2.patch(url, json={"status": "done"}, headers=auth).status_code == 404


def test_old_database_gets_status_columns():
    import sqlite3

    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "vieja.sqlite")
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE wishes (id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT NOT NULL, title TEXT NOT NULL,
                  artist TEXT NOT NULL, album TEXT, year INTEGER, added_at INTEGER NOT NULL, UNIQUE(user, title, artist))""")
    db.execute("INSERT INTO wishes (user, title, artist, added_at) VALUES ('ana', 'Uno', 'X', 1)")
    db.commit()
    db.close()
    w = Wishlist(path)
    assert w.list("ana")[0]["status"] == "pending"
    assert w.set_status(1, "downloading", None)["status"] == "downloading"


def test_cabeceras_antiguas_x_sonora():
    """Las versiones anteriores de la app mandan X-Sonora-*: siguen valiendo."""
    c = make_client()
    old = {"X-Sonora-User": "ana", "X-Sonora-Token": "bueno", "X-Sonora-Salt": "abc"}
    assert c.get("/api/v1/wishlist", headers=old).status_code == 200
    bad = {"X-Sonora-User": "ana", "X-Sonora-Token": "malo", "X-Sonora-Salt": "abc"}
    assert c.get("/api/v1/wishlist", headers=bad).status_code == 401


def test_base_de_datos_antigua_se_renombra():
    with tempfile.TemporaryDirectory() as d:
        old = os.path.join(d, "sonora.sqlite")
        Wishlist(old).add("ana", "Tusa", "KAROL G", None, None)
        path = main._database(d)
        assert path.endswith("melvara.sqlite") and not os.path.exists(old)
        assert Wishlist(path).list("ana")[0]["title"] == "Tusa"


def test_limpieza_de_la_busqueda_de_lastfm():
    from app.lastfm import clean_search

    def t(title, artist, listeners):
        return {"title": title, "artist": artist, "listeners": listeners}

    raw = [
        t("tHe CuRe", "Olivia Rodrigo", 925018),
        t("the cure • Olivia Rodrigo", "Anúncios limitados, com o apoio de", 6),
        t("expectations", "Olivia Rodrigo", 641560),
        t("the cure - performance video", "Olivia Rodrigo", 135103),
        t("vampire", "Olivia Rodrigo", 1593533),
    ]
    out = clean_search("the cure olivia rodrigo", raw)
    assert [(i["title"], i["artist"]) for i in out] == [("tHe CuRe", "Olivia Rodrigo")]

    raw = [
        t("tHe CuRe", "Olivia Rodrigo", 925018),
        t("Lovesong", "The Cure", 897494),
        t("Lovesong - Remastered", "The Cure", 787378),
        t("Boys Don't Cry", "The Cure", 2507716),
        t("Lullaby", "The Cure", 1056639),
    ]
    out = clean_search("the cure", raw)
    titles = [i["title"] for i in out]
    assert titles[0] == "tHe CuRe"  # se llama como lo buscado
    assert titles.count("Lovesong") == 1 and "Lovesong - Remastered" not in titles
    assert "Lullaby" in titles  # «lullaby» no es basura si es el título de verdad
    assert titles[1] == "Boys Don't Cry"  # luego, por oyentes

    # Si nada tiene todas las palabras, no se queda vacío: se dan las limpias.
    out = clean_search("cancion inventada xyz", [t("Otra", "Alguien", 5000)])
    assert len(out) == 1
