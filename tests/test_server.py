"""Pruebas de Melvara Server (sin red: Navidrome y Last.fm se simulan)."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from starlette.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.auth import Auth, RateLimiter  # noqa: E402
from app.lastfm import LastFM  # noqa: E402
from app.extras import ExtraLists  # noqa: E402
from app.importer import ImportJobs, find_track, match_library, tokens  # noqa: E402
from app.reports import Reports  # noqa: E402
from app.wishlist import Wishlist  # noqa: E402


class FakeAuth(Auth):
    def __init__(self):
        super().__init__("http://navidrome.invalid")

    def _ask_navidrome(self, user, token, salt):
        return token == "bueno"

    def _ask_admin(self, user, token, salt):
        return user == "admin"


class FakeLastFM(LastFM):
    def __init__(self, key="clave"):
        super().__init__(key, "secreto" if key else "")
        self.sent = None

    async def charts(self, country, limit=100, kind=None, value=None):
        if kind and value == "nada":
            return []
        return [{"title": "Tusa", "artist": "KAROL G", "country": country, "kind": kind, "value": value}]

    async def search(self, query, limit=30):
        return [{"title": "Tan Lines", "artist": "C Marí", "album": "Sempre Cor", "year": None, "listeners": 1, "url": None}]


SERVICE = "s" * 40


def make_client(lastfm_key="clave", per_minute=100, service_token=""):
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "s.sqlite")
    main.configure(FakeAuth(), RateLimiter(per_minute), Wishlist(db), FakeLastFM(lastfm_key),
                   service_token, Reports(db), ExtraLists(db))
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


def test_denuncias_las_ve_solo_un_administrador():
    c = make_client()
    assert c.get("/api/v1/status").json()["reports"] is True
    r = c.post("/api/v1/reports", json={"reported": "pepe", "reason": "offensive", "details": "Foto ofensiva",
                                        "playlistId": "p1", "playlistName": "Lo mejor"}, headers=headers("ana"))
    assert r.status_code == 200 and r.json()["item"]["reporter"] == "ana"
    item_id = r.json()["item"]["id"]
    assert c.post("/api/v1/reports", json={"reported": "pepe", "reason": "nope"}, headers=headers("ana")).status_code == 400
    assert c.post("/api/v1/reports", json={"reported": "ana", "reason": "spam"}, headers=headers("ana")).status_code == 400
    assert c.get("/api/v1/reports", headers=headers("ana")).status_code == 403
    items = c.get("/api/v1/reports", headers=headers("admin")).json()["items"]
    assert [i["reported"] for i in items] == ["pepe"] and items[0]["playlistName"] == "Lo mejor"
    assert c.patch(f"/api/v1/reports/{item_id}", headers=headers("ana")).status_code == 403
    done = c.patch(f"/api/v1/reports/{item_id}", headers=headers("admin")).json()["item"]
    assert done["resolvedBy"] == "admin" and done["resolvedAt"]
    assert c.get("/api/v1/reports", headers=headers("admin")).json()["items"] == []
    assert len(c.get("/api/v1/reports", params={"all": "1"}, headers=headers("admin")).json()["items"]) == 1
    assert c.patch("/api/v1/reports/999", headers=headers("admin")).status_code == 404


def test_listas_extra_las_pone_un_administrador_y_las_ven_todos():
    c = make_client()
    assert c.get("/api/v1/status").json()["extraLists"] is True
    nueva = {"kind": "tag", "value": "reggaeton", "title": "Reggaeton"}
    assert c.post("/api/v1/charts/extra", json=nueva, headers=headers("ana")).status_code == 403
    r = c.post("/api/v1/charts/extra", json=nueva, headers=headers("admin"))
    assert r.status_code == 200 and r.json()["item"]["title"] == "Reggaeton"
    item_id = r.json()["item"]["id"]
    assert c.post("/api/v1/charts/extra", json=nueva, headers=headers("admin")).status_code == 400  # repetida
    assert c.post("/api/v1/charts/extra", json={"kind": "tag", "value": "nada"}, headers=headers("admin")).status_code == 400
    assert c.post("/api/v1/charts/extra", json={"kind": "otra", "value": "x"}, headers=headers("admin")).status_code == 400
    # La ven todos.
    assert [i["value"] for i in c.get("/api/v1/charts/extra", headers=headers("ana")).json()["items"]] == ["reggaeton"]
    # Sus canciones, por la ruta de siempre.
    tracks = c.get("/api/v1/lastfm/charts", params={"kind": "tag", "value": "reggaeton"}, headers=headers("ana")).json()["tracks"]
    assert tracks[0]["kind"] == "tag" and tracks[0]["value"] == "reggaeton"
    # Cambiar y borrar: solo administradores.
    cambio = {"kind": "tag", "value": "reggaeton", "title": "Reggaetón"}
    assert c.patch(f"/api/v1/charts/extra/{item_id}", json=cambio, headers=headers("ana")).status_code == 403
    assert c.patch(f"/api/v1/charts/extra/{item_id}", json=cambio, headers=headers("admin")).json()["item"]["title"] == "Reggaetón"
    assert c.delete(f"/api/v1/charts/extra/{item_id}", headers=headers("admin")).json()["deleted"] is True
    assert c.get("/api/v1/charts/extra", headers=headers("ana")).json()["items"] == []


class FakeNavidrome:
    """Navidrome de mentira para probar la importación (search3, playlists)."""

    def __init__(self):
        self.songs = [
            {"id": "s1", "title": "Tusa", "artist": "KAROL G", "album": "KG0516"},
            {"id": "s2", "title": "Despacito (feat. Daddy Yankee)", "artist": "Luis Fonsi", "album": "Singles"},
            {"id": "s3", "title": "Despacito", "artist": "Luis Fonsi", "album": "VIDA"},
            {"id": "s4", "title": "90%", "artist": "C Marí", "album": "Sempre Cor"},
            {"id": "s5", "title": "90%", "artist": "C Marí", "album": "Sempre Cor (Deluxe)"},
            {"id": "s9", "title": "Otra", "artist": "Alguien", "album": "X"},
        ]
        self.playlists = {"p1": {"name": "Melvara · Mi biblioteca", "owner": "ana", "ids": ["s9"]}}

    def handler(self, request):
        import httpx as _h
        from urllib.parse import parse_qsl
        params = dict(request.url.params)
        pairs = list(request.url.params.multi_items())
        if request.method == "POST":
            pairs = parse_qsl(request.content.decode())
            params = dict(pairs)
        ep = request.url.path.rsplit("/", 1)[-1].replace(".view", "")
        ok = {"status": "ok"}
        if ep == "search3":
            words = tokens(params["query"])
            found = [s for s in self.songs if all(w in tokens(f"{s['title']} {s['artist']} {s['album']}") for w in words)]
            ok["searchResult3"] = {"song": found}
        elif ep == "getPlaylists":
            ok["playlists"] = {"playlist": [{"id": k, "name": v["name"], "owner": v["owner"]} for k, v in self.playlists.items()]}
        elif ep == "getPlaylist":
            byid = {x["id"]: x for x in self.songs}
            ok["playlist"] = {"entry": [byid.get(i, {"id": i}) for i in self.playlists[params["id"]]["ids"]]}
        elif ep == "createPlaylist":
            ids = [v for k, v in pairs if k == "songId"]
            if "playlistId" in params:
                self.playlists[params["playlistId"]]["ids"] = ids
            else:
                new = f"p{len(self.playlists) + 1}"
                self.playlists[new] = {"name": params["name"], "owner": "ana", "ids": ids}
                ok["playlist"] = {"id": new}
        elif ep == "updatePlaylist":
            self.playlists[params["playlistId"]]["ids"] += [v for k, v in pairs if k == "songIdToAdd"]
        return _h.Response(200, json={"subsonic-response": ok})


def test_importacion_en_el_servidor():
    import time as _t
    import httpx as _h
    fake = FakeNavidrome()
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "s.sqlite")
    main.configure(FakeAuth(), RateLimiter(100), Wishlist(db), FakeLastFM(), "", Reports(db), ExtraLists(db),
                   ImportJobs(db, "http://navidrome.invalid", transport=_h.MockTransport(fake.handler)))
    c = TestClient(main.app)
    assert c.get("/api/v1/status").json()["imports"] is True
    body = {
        "provider": "appleMusic",
        "songs": [
            {"key": "k1", "title": "Tusa", "artist": "KAROL G, Nicki Minaj", "album": "KG0516"},
            {"key": "k2", "title": "Despacito", "artist": "Luis Fonsi", "album": "VIDA"},
            {"key": "k3", "title": "90%", "artist": "C Marí & BLNCO", "album": "Sempre Cor (Deluxe)"},
            {"key": "k4", "title": "No existe", "artist": "Nadie", "album": ""},
        ],
        "library": ["k1", "k2", "k3", "k4"],
        "playlists": [{"name": "Favoritas", "keys": ["k3", "k1"]}],
        # «Otra» ya estaba en la biblioteca (de una importación antigua, sin su id apuntado): se reconoce por
        # título y álbum y va en su sitio, al final, en vez de quedarse delante.
        "reorderLibrary": True, "order": [
            {"key": "k3", "title": "90%", "album": "Sempre Cor (Deluxe)", "added": 1_700_000_000},
            {"key": "k2", "title": "Despacito", "album": "VIDA", "added": 1_700_000_100},
            {"key": "k1", "title": "Tusa", "album": "KG0516", "added": 1_700_000_200},
            {"key": "k9", "title": "Otra", "album": "X", "added": 1_700_000_300}],
    }
    with c:
        job = c.post("/api/v1/import/jobs", json=body, headers=headers("ana")).json()["job"]
        assert job["status"] == "running" and job["total"] == 4
        for _ in range(100):
            job = c.get(f"/api/v1/import/jobs/{job['id']}", headers=headers("ana")).json()["job"]
            if job["status"] != "running":
                break
            _t.sleep(0.05)
    assert job["status"] == "done", job
    r = job["result"]
    assert r["found"] == 3 and r["missing"] == ["k4"] and r["addedToLibrary"] == 3 and r["playlistsCreated"] == 1
    # Versión exacta y álbum correcto; «Otra» (k9) ya estaba en la biblioteca y también se apunta.
    assert r["matches"] == {"k1": "s1", "k2": "s3", "k3": "s5", "k9": "s9"}
    # Biblioteca: lo que ya tenía primero y lo importado en el orden del proveedor.
    assert fake.playlists["p1"]["ids"] == ["s5", "s3", "s1", "s9"] and r["reordered"] is True
    assert fake.playlists["p2"] == {"name": "Favoritas", "owner": "ana", "ids": ["s5", "s1"]}
    # Nadie más ve tu importación.
    assert c.get(f"/api/v1/import/jobs/{job['id']}", headers=headers("pepe")).status_code == 404


def test_una_cancion_que_falla_no_para_la_importacion():
    import time as _t
    import httpx as _h
    fake = FakeNavidrome()
    real = fake.handler

    def flaky(request):
        # «Tusa» siempre falla (como una búsqueda que tarda demasiado); las demás van bien.
        if "search3" in request.url.path and "tusa" in str(request.url).lower():
            raise _h.ReadTimeout("lento")
        return real(request)

    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "s.sqlite")
    main.configure(FakeAuth(), RateLimiter(100), Wishlist(db), FakeLastFM(), "", Reports(db), ExtraLists(db),
                   ImportJobs(db, "http://navidrome.invalid", transport=_h.MockTransport(flaky)))
    c = TestClient(main.app)
    body = {"songs": [{"key": "k1", "title": "Tusa", "artist": "KAROL G"},
                      {"key": "k2", "title": "Despacito", "artist": "Luis Fonsi", "album": "VIDA"}],
            "library": ["k1", "k2"]}
    with c:
        job = c.post("/api/v1/import/jobs", json=body, headers=headers("ana")).json()["job"]
        for _ in range(100):
            job = c.get(f"/api/v1/import/jobs/{job['id']}", headers=headers("ana")).json()["job"]
            if job["status"] != "running":
                break
            _t.sleep(0.05)
    assert job["status"] == "done", job
    assert job["result"]["found"] == 1 and job["result"]["missing"] == ["k1"]


def test_biblioteca_en_una_sola_linea_de_tiempo():
    """Apple Music hoy, Melvara ayer, Apple Music antes de ayer → en ese orden (de más antigua a más nueva)."""
    import time as _t
    import httpx as _h
    fake = FakeNavidrome()
    fake.songs[5]["created"] = "2020-01-01T00:00:00Z"                     # s9: llegó al servidor hace años
    fake.playlists["p1"]["ids"] = ["s9", "s4"]                             # s4: añadida en Melvara con el +
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "s.sqlite")
    main.configure(FakeAuth(), RateLimiter(100), Wishlist(db), FakeLastFM(), "", Reports(db), ExtraLists(db),
                   ImportJobs(db, "http://navidrome.invalid", transport=_h.MockTransport(fake.handler)))
    c = TestClient(main.app)
    day = 86400
    now = 1_800_000_000
    body = {
        "songs": [{"key": "k1", "title": "Tusa", "artist": "KAROL G", "album": "KG0516"},
                  {"key": "k2", "title": "Despacito", "artist": "Luis Fonsi", "album": "VIDA"}],
        "library": ["k1", "k2"], "reorderLibrary": True,
        "order": [{"key": "k2", "title": "Despacito", "album": "VIDA", "added": now - 2 * day},
                  {"key": "k1", "title": "Tusa", "album": "KG0516", "added": now}],
        "libraryDates": {"s4": now - day},
    }
    with c:
        job = c.post("/api/v1/import/jobs", json=body, headers=headers("ana")).json()["job"]
        for _ in range(100):
            job = c.get(f"/api/v1/import/jobs/{job['id']}", headers=headers("ana")).json()["job"]
            if job["status"] != "running":
                break
            _t.sleep(0.05)
    assert job["status"] == "done", job
    # s9 (2020) · Despacito (antes de ayer, Apple Music) · s4 (ayer, Melvara) · Tusa (hoy, Apple Music)
    assert fake.playlists["p1"]["ids"] == ["s9", "s3", "s4", "s1"]


def test_reconoce_tu_biblioteca_aunque_los_titulos_no_sean_identicos():
    """Apple Music dice «Despacito» y el archivo «Despacito - Remastered 2011» en «VIDA (Deluxe)»: es la misma."""
    entries = [
        {"id": "a", "title": "Despacito - Remastered 2011", "artist": "Luis Fonsi", "album": "VIDA (Deluxe)"},
        {"id": "b", "title": "Tusa (feat. Nicki Minaj)", "artist": "KAROL G", "album": "KG0516"},
        {"id": "c", "title": "Tusa", "artist": "Otra Persona", "album": "X"},
        {"id": "d", "title": "Hola", "artist": "Alguien", "album": "Uno"},
        {"id": "e", "title": "Hola", "artist": "Alguien", "album": "Dos"},
    ]
    order = [
        {"key": "k1", "title": "Despacito", "artist": "Luis Fonsi & Daddy Yankee", "album": "VIDA"},
        {"key": "k2", "title": "Tusa", "artist": "KAROL G, Nicki Minaj", "album": "KG0516"},
        {"key": "k3", "title": "Hola", "artist": "Alguien", "album": "Dos"},
        {"key": "k4", "title": "No está", "artist": "Nadie", "album": ""},
    ]
    got = match_library(entries, order, {})
    assert got == {"k1": "a", "k2": "b", "k3": "e"}
    # Lo ya sabido manda (si sigue en la biblioteca).
    assert match_library(entries, order, {"k3": "d"})["k3"] == "d"
