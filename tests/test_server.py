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
    fake.songs[5]["created"] = "2099-01-01T00:00:00Z"                     # s9: sin fecha (aunque llegara «ayer»)
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
    # s9 (sin fecha: con las más antiguas) · Despacito (antes de ayer, Apple Music) · s4 (ayer, Melvara) · Tusa (hoy)
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


def test_escuchar_en_grupo():
    c = make_client()
    main.state.jams = main.Jams()
    assert c.get("/api/v1/status").json()["jam"] is True
    jam = c.post("/api/v1/jam", headers=headers("ana")).json()["jam"]
    code = jam["code"]
    assert jam["isHost"] is True and jam["members"] == []
    # Sin unirse no se puede añadir; quien no tiene cuenta en Navidrome, ni entra.
    assert c.post(f"/api/v1/jam/{code}/add", json={"tracks": [{"id": "s1"}]}, headers=headers("pepe")).status_code == 403
    assert c.post(f"/api/v1/jam/{code}/join", headers=headers("pepe", token="malo")).status_code == 401
    assert c.post(f"/api/v1/jam/{code}/join", headers=headers("pepe")).json()["jam"]["members"] == ["pepe"]
    added = c.post(f"/api/v1/jam/{code}/add", json={"tracks": [{"id": "s1", "title": "Tusa", "artist": "KAROL G"}]},
                   headers=headers("pepe")).json()
    assert added == {"added": 1}
    # El anfitrión recoge lo añadido y cuenta qué suena.
    pending = c.get(f"/api/v1/jam/{code}/pending", params={"after": 0}, headers=headers("ana")).json()
    assert pending["adds"][0]["by"] == "pepe" and pending["adds"][0]["track"]["title"] == "Tusa"
    assert c.get(f"/api/v1/jam/{code}/pending", params={"after": pending["seq"]}, headers=headers("ana")).json()["adds"] == []
    assert c.get(f"/api/v1/jam/{code}/pending", headers=headers("pepe")).status_code == 403   # solo el anfitrión
    c.post(f"/api/v1/jam/{code}/state", json={"nowPlaying": {"id": "s0", "title": "Ahora"}, "queue": [{"id": "s1", "title": "Tusa"}]},
           headers=headers("ana"))
    seen = c.get(f"/api/v1/jam/{code}", headers=headers("pepe")).json()["jam"]
    assert seen["nowPlaying"]["title"] == "Ahora" and seen["queue"][0]["addedBy"] == "pepe" and seen["isHost"] is False
    # Cambiar de canción y adelantar: solo si el anfitrión lo permite (por defecto, no).
    assert seen["perms"] == {"skip": False, "seek": False}
    assert c.post(f"/api/v1/jam/{code}/control", json={"action": "next"}, headers=headers("pepe")).status_code == 403
    perms = c.post(f"/api/v1/jam/{code}/perms", json={"skip": True}, headers=headers("ana")).json()["jam"]["perms"]
    assert perms == {"skip": True, "seek": False}
    assert c.post(f"/api/v1/jam/{code}/perms", json={"seek": True}, headers=headers("pepe")).status_code == 403
    assert c.post(f"/api/v1/jam/{code}/control", json={"action": "next"}, headers=headers("pepe")).json()["ok"] is True
    assert c.post(f"/api/v1/jam/{code}/control", json={"action": "seek", "position": 30}, headers=headers("pepe")).status_code == 403
    c.post(f"/api/v1/jam/{code}/perms", json={"seek": True}, headers=headers("ana"))
    assert c.post(f"/api/v1/jam/{code}/control", json={"action": "seek"}, headers=headers("pepe")).status_code == 400
    assert c.post(f"/api/v1/jam/{code}/control", json={"action": "borrar"}, headers=headers("pepe")).status_code == 400
    c.post(f"/api/v1/jam/{code}/control", json={"action": "seek", "position": 30}, headers=headers("pepe"))
    after = pending["seq"]
    commands = c.get(f"/api/v1/jam/{code}/pending", params={"after": after}, headers=headers("ana")).json()["commands"]
    assert [(x["action"], x["by"], x.get("position")) for x in commands] == [("next", "pepe", None), ("seek", "pepe", 30.0)]
    # Por dónde va la canción del anfitrión.
    c.post(f"/api/v1/jam/{code}/state", json={"nowPlaying": {"id": "s0"}, "queue": [], "position": 12.5, "playing": True},
           headers=headers("ana"))
    seen = c.get(f"/api/v1/jam/{code}", headers=headers("pepe")).json()["jam"]
    assert seen["position"] == 12.5 and seen["playing"] is True and seen["now"] >= seen["positionAt"]
    # Quitar a alguien: ya no puede volver.
    c.post(f"/api/v1/jam/{code}/kick", json={"user": "pepe"}, headers=headers("ana"))
    assert c.post(f"/api/v1/jam/{code}/join", headers=headers("pepe")).status_code == 403
    # Cerrar: se acaba para todos.
    assert c.delete(f"/api/v1/jam/{code}", headers=headers("ana")).json()["ended"] is True
    assert c.get(f"/api/v1/jam/{code}", headers=headers("ana")).status_code == 404


def test_navidrome_ocupado_se_reintenta():
    """«database is locked» al añadir a la biblioteca: se espera y se repite en vez de parar la importación."""
    import time as _t
    import httpx as _h
    from app.importer import Navidrome
    fake = FakeNavidrome()
    real = fake.handler
    locked = {"left": 2}

    def busy(request):
        if "updatePlaylist" in request.url.path and locked["left"] > 0:
            locked["left"] -= 1
            return _h.Response(200, json={"subsonic-response": {"status": "failed", "error": {
                "code": 0, "message": "Internal Server Error: database is locked"}}})
        return real(request)

    old = Navidrome.BUSY_RETRY_DELAYS
    Navidrome.BUSY_RETRY_DELAYS = (0, 0, 0)
    try:
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "s.sqlite")
        main.configure(FakeAuth(), RateLimiter(100), Wishlist(db), FakeLastFM(), "", Reports(db), ExtraLists(db),
                       ImportJobs(db, "http://navidrome.invalid", transport=_h.MockTransport(busy)))
        c = TestClient(main.app)
        body = {"songs": [{"key": "k2", "title": "Despacito", "artist": "Luis Fonsi", "album": "VIDA"}],
                "library": ["k2"]}
        with c:
            job = c.post("/api/v1/import/jobs", json=body, headers=headers("ana")).json()["job"]
            for _ in range(100):
                job = c.get(f"/api/v1/import/jobs/{job['id']}", headers=headers("ana")).json()["job"]
                if job["status"] != "running":
                    break
                _t.sleep(0.05)
        assert job["status"] == "done", job
        assert locked["left"] == 0 and fake.playlists["p1"]["ids"] == ["s9", "s3"]   # añadida una sola vez
    finally:
        Navidrome.BUSY_RETRY_DELAYS = old


def test_social_personas_y_lo_que_escuchan():
    import json as _j
    import httpx as _h
    from app.social import Social
    profiles = {
        "ana": {"following": ["pepe", "luis", "eva"], "accepted": [], "isPrivate": False},
        "pepe": {"following": ["ana"], "accepted": []},                      # mutuo con ana
        "luis": {"following": [], "accepted": []},                           # ana le sigue, él no
        "eva": {"following": ["ana"], "isPrivate": True, "accepted": []},    # privada: no ha aceptado a ana
    }

    def handler(request):
        playlists = [{"id": f"p-{u}", "name": "Melvara · Perfil", "owner": u, "public": True,
                      "comment": _j.dumps(p)} for u, p in profiles.items()]
        return _h.Response(200, json={"subsonic-response": {"status": "ok", "playlists": {"playlist": playlists}}})

    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "s.sqlite")
    social = Social(db, "http://navidrome.invalid", transport=_h.MockTransport(handler))
    main.configure(FakeAuth(), RateLimiter(100), Wishlist(db), FakeLastFM(), "", Reports(db), ExtraLists(db),
                   social=social)
    c = TestClient(main.app)
    assert c.get("/api/v1/status").json()["social"] is True
    # Personas: quien usa Melvara con este servidor (cualquier petición cuenta); los ocultos no salen.
    for u in ("ana", "pepe", "luis", "eva", "oculto"):
        c.get("/api/v1/social/people", headers=headers(u))
    c.post("/api/v1/social/visibility", json={"hidden": True}, headers=headers("oculto"))
    assert c.get("/api/v1/social/people", headers=headers("ana")).json()["people"] == ["eva", "luis", "pepe"]

    song = {"id": "s1", "title": "Tusa", "artist": "KAROL G", "duration": 200000}
    # Pepe comparte y escucha; ana aún no comparte: no ve nada.
    c.post("/api/v1/social/listening/share", json={"share": True}, headers=headers("pepe"))
    c.post("/api/v1/social/listening", json={"track": song}, headers=headers("pepe"))
    assert c.get("/api/v1/social/listening", headers=headers("ana")).json() == {"sharing": False, "friends": []}
    # Ana comparte: ve a pepe (mutuo) escuchando.
    c.post("/api/v1/social/listening/share", json={"share": True}, headers=headers("ana"))
    friends = c.get("/api/v1/social/listening", headers=headers("ana")).json()["friends"]
    assert [f["user"] for f in friends] == ["pepe"] and friends[0]["nowPlaying"]["title"] == "Tusa"
    # Luis y eva comparten, pero no son mutuos con ana (luis no la sigue; eva no la ha aceptado).
    for u in ("luis", "eva"):
        c.post("/api/v1/social/listening/share", json={"share": True}, headers=headers(u))
        c.post("/api/v1/social/listening", json={"track": song}, headers=headers(u))
    social._profiles.clear()
    assert [f["user"] for f in c.get("/api/v1/social/listening", headers=headers("ana")).json()["friends"]] == ["pepe"]
    # Las 5 últimas, sin repetir.
    for i in range(7):
        c.post("/api/v1/social/listening", json={"track": {"id": f"t{i}", "title": f"T{i}"}}, headers=headers("pepe"))
    pepe = c.get("/api/v1/social/listening", headers=headers("ana")).json()["friends"][0]
    assert [t["id"] for t in pepe["recent"]] == ["t6", "t5", "t4", "t3", "t2"]
    # Pepe deja de compartir: desaparece y se borra lo suyo.
    c.post("/api/v1/social/listening/share", json={"share": False}, headers=headers("pepe"))
    assert c.get("/api/v1/social/listening", headers=headers("ana")).json()["friends"] == []
    # Un bloqueo corta lo mutuo.
    c.post("/api/v1/social/listening/share", json={"share": True}, headers=headers("pepe"))
    c.post("/api/v1/social/listening", json={"track": song}, headers=headers("pepe"))
    profiles["pepe"]["blocked"] = ["ana"]
    social._profiles.clear()
    assert c.get("/api/v1/social/listening", headers=headers("ana")).json()["friends"] == []


def test_navidrome_que_no_responde_no_deja_fuera():
    """Si Navidrome no responde, 503 (no «acceso no válido») y no se recuerda: al volver, entra a la primera."""
    from app.auth import NavidromeUnavailable

    class FlakyAuth(Auth):
        def __init__(self):
            super().__init__("http://navidrome.invalid")
            self.down = True

        def _ask_navidrome(self, user, token, salt):
            if self.down:
                raise NavidromeUnavailable()
            return True

    auth = FlakyAuth()
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "s.sqlite")
    main.configure(auth, RateLimiter(100), Wishlist(db), FakeLastFM(), "", Reports(db), ExtraLists(db))
    c = TestClient(main.app)
    r = c.get("/api/v1/wishlist", headers=headers("ana"))
    assert r.status_code == 503 and "no responde" in r.json()["error"]
    auth.down = False
    assert c.get("/api/v1/wishlist", headers=headers("ana")).status_code == 200


# ------------------------------------------------------------------ fuentes y web de administración (2.11)

from app import admin as admin_web  # noqa: E402
from app.sources import SourceError, Sources  # noqa: E402


class FakeSource:
    def __init__(self, name, fail=False):
        self.name, self.fail, self.calls = name, fail, 0

    async def chart(self, code, limit=100):
        self.calls += 1
        if self.fail:
            raise SourceError(f"{self.name} caído")
        if self.name == "apple" and not code:
            raise SourceError("sin mundial")
        return [{"title": f"{self.name}-{code or 'world'}", "artist": "X", "album": None}]

    async def search(self, query, limit=30):
        if self.fail:
            raise SourceError("caído")
        return [{"title": "Tusa", "artist": "KAROL G", "album": "KG0516", "year": None, "listeners": 5, "url": None}]

    async def artist_top(self, artist, limit=50):
        return [{"title": f"top-{artist}", "artist": artist, "album": None}]


def make_sources_client(lastfm_key="", apple_fail=False, deezer_fail=False):
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "s.sqlite")
    from app.sources import Settings
    lastfm = FakeLastFM(lastfm_key)
    apple, deezer = FakeSource("apple", apple_fail), FakeSource("deezer", deezer_fail)
    sources = Sources(lastfm, Settings(db), db, deezer=deezer, apple=apple, listenbrainz=FakeSource("listenbrainz"))
    main.configure(FakeAuth(), RateLimiter(100), Wishlist(db), lastfm, "", Reports(db), ExtraLists(db), sources=sources)
    return TestClient(main.app), sources, apple, deezer


def test_sin_lastfm_se_busca_en_deezer_y_las_listas_salen_de_apple():
    c, sources, apple, deezer = make_sources_client()
    st = c.get("/api/v1/status").json()
    assert st["lastfm"] is True and st["lastfmAuth"] is False  # las apps instaladas siguen viendo búsqueda y listas
    assert st["sources"]["search"] == "deezer" and st["sources"]["charts"] == "apple"
    r = c.get("/api/v1/lastfm/search", params={"q": "tusa"}, headers=headers()).json()
    assert r["source"] == "deezer" and r["results"][0]["title"] == "Tusa"
    # La app manda el país en inglés («spain»); también vale el código.
    r = c.get("/api/v1/lastfm/charts", params={"country": "spain"}, headers=headers()).json()
    assert r["source"] == "apple" and r["tracks"][0]["title"] == "apple-es"
    assert c.get("/api/v1/lastfm/charts", params={"country": "es"}, headers=headers()).json()["source"] == "apple"
    assert apple.calls == 1  # la segunda vez, de lo guardado
    # Apple no tiene mundial: sale de la siguiente (Deezer).
    assert c.get("/api/v1/lastfm/charts", headers=headers()).json()["source"] == "deezer"
    assert c.get("/api/v1/lastfm/charts", params={"country": "narnia"}, headers=headers()).status_code == 400


def test_si_la_fuente_falla_se_usa_la_siguiente_o_la_ultima_buena():
    c, sources, apple, deezer = make_sources_client(apple_fail=True)
    assert c.get("/api/v1/lastfm/charts", params={"country": "spain"}, headers=headers()).json()["source"] == "deezer"
    # Todo caído: se sigue dando la última buena.
    deezer.fail = True
    sources.forget()
    r = c.get("/api/v1/lastfm/charts", params={"country": "spain"}, headers=headers())
    assert r.status_code == 200 and r.json()["tracks"][0]["title"] == "deezer-es"


def test_listas_extra_sin_lastfm():
    c, *_ = make_sources_client()
    # Etiquetas: solo Last.fm. Artista: Deezer.
    assert c.post("/api/v1/charts/extra", json={"kind": "tag", "value": "rock"}, headers=headers("admin")).status_code == 400
    assert c.post("/api/v1/charts/extra", json={"kind": "artist", "value": "Rosalía"}, headers=headers("admin")).status_code == 200
    r = c.get("/api/v1/lastfm/charts", params={"kind": "artist", "value": "Rosalía"}, headers=headers()).json()
    assert r["source"] == "deezer" and r["tracks"][0]["title"] == "top-Rosalía"


def _login(c, user="admin", password="bueno"):
    # FakeAuth da por bueno el token «bueno»: se simula que la contraseña es correcta.
    original = admin_web.hashlib.md5
    admin_web.hashlib.md5 = lambda data: type("H", (), {"hexdigest": lambda self: password})()
    try:
        return c.post("/admin/login", data={"user": user, "password": "x"}, follow_redirects=False)
    finally:
        admin_web.hashlib.md5 = original


def test_web_de_administracion():
    c, sources, apple, deezer = make_sources_client()
    r = c.get("/admin")
    assert r.status_code == 200 and "Entrar" in r.text and "frame-ancestors" in r.headers["content-security-policy"]
    assert _login(c, "ana").headers["location"].endswith("m=notadmin")
    assert _login(c, "admin", "malo").headers["location"].endswith("m=bad")
    r = _login(c)
    assert r.status_code == 303 and admin_web.COOKIE in r.cookies
    page = c.get("/admin").text
    assert "Listas de éxitos" in page and "Last.fm Pro" in page
    csrf = page.split('name="csrf" value="')[1].split('"')[0]
    # Sin el token del formulario no se guarda nada.
    assert c.post("/admin/settings", data={"search": "lastfm"}, follow_redirects=False).headers["location"].endswith("m=csrf")
    # Desde otra web, tampoco.
    other = c.post("/admin/settings", data={"csrf": csrf, "charts": "deezer"}, headers={"Origin": "https://malo.example"},
                   follow_redirects=False)
    assert other.headers["location"].endswith("m=csrf")
    r = c.post("/admin/settings", data={"csrf": csrf, "search": "deezer", "charts": "deezer", "list_es": "apple",
                                        "list_world": "apple", "lastfm_key": "nueva", "lastfm_on": "1"},
               follow_redirects=False)
    assert r.headers["location"].endswith("m=saved")
    st = c.get("/api/v1/status").json()["sources"]
    assert st["charts"] == "deezer" and st["lists"]["es"] == "apple"
    assert st["lists"]["world"] == "deezer"  # Apple no tiene mundial: no se guarda
    assert sources.lastfm.api_key == "nueva" and c.get("/api/v1/status").json()["lastfm"] is True
    assert c.get("/api/v1/lastfm/charts", params={"country": "mexico"}, headers=headers()).json()["source"] == "deezer"
    # Desactivar Last.fm.
    c.post("/admin/settings", data={"csrf": csrf, "search": "lastfm"}, follow_redirects=False)
    assert sources.lastfm.api_key == "" and c.get("/api/v1/status").json()["sources"]["search"] == "deezer"
    # Salir.
    c.post("/admin/logout", follow_redirects=False)
    assert "Entrar" in c.get("/admin").text


def test_busquedas_iguales_salen_de_memoria():
    c, sources, apple, deezer = make_sources_client()
    calls = []
    original = deezer.search

    async def counted(query, limit=30):
        calls.append(query)
        return await original(query, limit)

    deezer.search = counted
    for q in ("Tusa", "tusa ", "TUSA"):
        assert c.get("/api/v1/lastfm/search", params={"q": q}, headers=headers()).json()["source"] == "deezer"
    assert calls == ["Tusa"]


def test_version_de_la_app_de_cada_persona():
    from app.social import Social
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "s.sqlite")
    main.configure(FakeAuth(), RateLimiter(100), Wishlist(db), FakeLastFM(), "", Reports(db), ExtraLists(db),
                   social=Social(db, "http://navidrome.invalid"))
    c = TestClient(main.app)
    ua = {"User-Agent": "Melvara/51 CFNetwork/3860.100.1 Darwin/25.1.0"}
    c.get("/api/v1/wishlist", headers={**headers("ana"), **ua})
    c.get("/api/v1/wishlist", headers={**headers("luis"), "X-Melvara-App": "1.3 (52)"})
    c.get("/api/v1/wishlist", headers=headers("eva"))  # sin versión
    assert c.get("/api/v1/admin/people", headers=headers("ana")).status_code == 403
    people = {p["user"]: p["app"] for p in c.get("/api/v1/admin/people", headers=headers("admin")).json()["people"]}
    assert people["ana"] == "(51)" and people["luis"] == "1.3 (52)" and people["eva"] is None
    # Al actualizar la app se apunta al momento (sin esperar a la hora).
    c.get("/api/v1/wishlist", headers={**headers("ana"), "X-Melvara-App": "1.3 (53)"})
    c.get("/api/v1/wishlist", headers=headers("ana"))  # una petición sin versión no la borra
    people = {p["user"]: p["app"] for p in c.get("/api/v1/admin/people", headers=headers("admin")).json()["people"]}
    assert people["ana"] == "1.3 (53)"


def test_horas_de_cache_desde_la_web():
    c, sources, apple, deezer = make_sources_client()
    _login(c)
    csrf = c.get("/admin").text.split('name="csrf" value="')[1].split('"')[0]
    c.post("/admin/settings", data={"csrf": csrf, "search_hours": "0", "chart_hours": "99"}, follow_redirects=False)
    assert sources.search_ttl == 0 and sources.chart_ttl == 48 * 3600
    calls = []
    original = deezer.search

    async def counted(query, limit=30):
        calls.append(query)
        return await original(query, limit)

    deezer.search = counted
    c.get("/api/v1/lastfm/search", params={"q": "tusa"}, headers=headers())
    c.get("/api/v1/lastfm/search", params={"q": "tusa"}, headers=headers())
    assert len(calls) == 2  # con 0 horas no se guarda


def test_ultima_cancion_y_version_minima():
    from app.social import Social
    from app.sources import Settings
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "s.sqlite")
    lastfm = FakeLastFM()
    main.configure(FakeAuth(), RateLimiter(100), Wishlist(db), lastfm, "", Reports(db), ExtraLists(db),
                   social=Social(db, "http://navidrome.invalid"), sources=Sources(lastfm, Settings(db), db))
    c = TestClient(main.app)
    assert c.get("/api/v1/status").json()["lastPlayed"] is True
    assert c.post("/api/v1/played", json={"track": {"artist": "X"}}, headers=headers("ana")).status_code == 400
    c.post("/api/v1/played", json={"track": {"title": "Tusa", "artist": "KAROL G", "album": "KG0516", "extra": 1}},
           headers=headers("ana"))
    people = {p["user"]: p for p in c.get("/api/v1/admin/people", headers=headers("admin")).json()["people"]}
    assert people["ana"]["lastTrack"] == {"title": "Tusa", "artist": "KAROL G", "album": "KG0516"}
    assert people["ana"]["lastPlayedAt"]
    # Versión mínima desde la web.
    _login(c)
    page = c.get("/admin").text
    assert "Tusa" in page and "Versión mínima" in page
    csrf = page.split('name="csrf" value="')[1].split('"')[0]
    c.post("/admin/settings", data={"csrf": csrf, "min_app": "1.3 (53)"}, follow_redirects=False)
    assert c.get("/api/v1/status").json()["minApp"] == "1.3 (53)"
    c.post("/admin/settings", data={"csrf": csrf, "min_app": "cualquier cosa"}, follow_redirects=False)
    assert c.get("/api/v1/status").json()["minApp"] == "1.3 (53)"  # lo que no es una versión no se guarda
    c.post("/admin/settings", data={"csrf": csrf, "min_app": ""}, follow_redirects=False)
    assert c.get("/api/v1/status").json()["minApp"] is None
