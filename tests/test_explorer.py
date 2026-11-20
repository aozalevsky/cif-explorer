import json
import threading
import urllib.request

import pytest
from conftest import edit

from cif_explorer.explorer import NotFound, Session, Views, _clean_text
from cif_explorer.server import make_server


@pytest.fixture
def session(mini_cif, mini_dic):
    s = Session(mini_cif, mini_dic)
    assert s.load()
    return s


def test_refs(session):
    v = Views(session)
    t = v.table("struct_asym")
    assert v.ref(t, 1) == 'k:["B"]'
    assert v.resolve(t, 'k:["B"]') == 1
    assert v.resolve(t, 'k:["Z"]') is None
    eps = v.table("entity_poly_seq")
    assert v.ref(eps, 2) == 'k:["1", "3", "ALA"]'


def test_row_detail_up_and_down(session):
    v = Views(session)
    row = v.row_detail("atom_site", 'k:["3"]')
    assert row["label"] == "GLY 2 CA · asym A"
    ups = {p["parent"]: p for p in row["parents"]}
    assert ups["entity_poly_seq"]["status"] == "ok"
    assert ups["entity_poly_seq"]["rows"][0]["ref"] == 'k:["1", "2", "GLY"]'
    assert ups["struct_asym"]["rows"][0]["label"].startswith("id=A")
    # water atom: residue number is '.', so the sequence link does not apply
    water = v.row_detail("atom_site", 'k:["5"]')
    st = {p["parent"]: p["status"] for p in water["parents"]}
    assert st["entity_poly_seq"] == "null" and st["entity"] == "ok"

    ent = v.row_detail("entity", 'k:["1"]')
    downs = {c["child"]: c["count"] for c in ent["children"]}
    assert downs["atom_site"] == 4 and downs["entity_poly_seq"] == 3
    link = next(c["link"] for c in ent["children"]
                if c["child"] == "atom_site")
    page = v.children("entity", 'k:["1"]', link, limit=2)
    assert page["count"] == 4 and len(page["rows"]) == 2


def test_broken_parent_flagged(session, mini_cif):
    edit(mini_cif, "3 CA GLY 2 A 1", "3 CA GLY 2 C 1")
    assert session.load()
    v = Views(session)
    row = v.row_detail("atom_site", 'k:["3"]')
    st = {p["parent"]: p["status"] for p in row["parents"]}
    assert st["struct_asym"] == "missing"
    page = v.table_page("atom_site")
    flags = page["rows"][2]["flags"]
    col = [it["item"] for it in page["items"]].index("label_asym_id")
    assert "no struct_asym row" in flags[col]


def test_table_page(session):
    v = Views(session)
    page = v.table_page("atom_site", limit=2, offset=2)
    assert [r["idx"] for r in page["rows"]] == [2, 3]
    assert page["total"] == 5
    page = v.table_page("atom_site", filters=[("label_comp_id", "ALA")])
    assert [r["idx"] for r in page["rows"]] == [0, 1, 3]
    page = v.table_page("atom_site", q="hoh")
    assert [r["idx"] for r in page["rows"]] == [4]
    page = v.table_page("atom_site", limit=2, around=3)
    assert page["offset"] == 2
    assert page["items"][6] == {"item": "cartn_x", "name": "Cartn_x",
                                "known": True, "mandatory": False}
    with pytest.raises(NotFound):
        v.table_page("nope")


def test_reload_keeps_refs(session, mini_cif):
    v1 = Views(session)
    ref = v1.ref(v1.table("struct_asym"), 1)
    # insert a row before B: index changes, key ref still resolves
    edit(mini_cif, "A 1 'chain A'\n", "A 1 'chain A'\nA2 1 'chain A2'\n")
    assert session.load()
    assert session.version == 2
    v2 = Views(session)
    assert v2.resolve(v2.table("struct_asym"), ref) == 2
    out = v2.resolve_refs([{"cat": "struct_asym", "ref": ref},
                           {"cat": "struct_asym", "ref": 'k:["Q"]'}])
    assert out[0]["idx"] == 2 and out[1] is None


def test_failed_reload_keeps_data(session, mini_cif):
    edit(mini_cif, "_entry.id MINI", "_entry.id\n;MINI")
    assert not session.load()
    assert session.state == "ready" and session.error
    assert Views(session).table("entry").row(0) == ["MINI"]
    edit(mini_cif, "_entry.id\n;MINI", "_entry.id MINI2")
    assert session.load()
    assert session.error is None
    assert Views(session).table("entry").row(0) == ["MINI2"]


def test_search(session):
    res = Views(session).search("glycine")
    assert [r["cat"] for r in res["results"]] == ["chem_comp"]


def test_clean_text():
    raw = ("              A line\n               wrapped here.\n\n"
           "               Next paragraph.\n                  _pre.formatted 1")
    assert _clean_text(raw) == ("A line wrapped here.\n\nNext paragraph.\n"
                                "   _pre.formatted 1")
    # base == 0 must still preserve indented preformatted lines
    raw0 = "First line\nSecond line\nThird line\n    _pre.formatted 1"
    assert _clean_text(raw0) == ("First line Second line Third line\n"
                                 "    _pre.formatted 1")
    assert _clean_text("\n\n") == ""


def test_duplicate_key_refs(session, mini_cif):
    edit(mini_cif, "B 2 ?", "A 2 ?")
    assert session.load()
    v = Views(session)
    t = v.table("struct_asym")
    r0 = v.ref(t, 0)
    r1 = v.ref(t, 1)
    assert r0 == 'k:["A"]'
    assert r1 == 'k1:["A"]'
    assert v.resolve(t, r0) == 0
    assert v.resolve(t, r1) == 1
    assert v.resolve(t, 'k2:["A"]') is None
    assert v.row_detail("struct_asym", r0)["idx"] == 0
    assert v.row_detail("struct_asym", r1)["idx"] == 1


def test_children_null_parent_key_and_bounds(session, mini_cif):
    # entity row 1 has id=2; change its id to '?' so no child matches
    edit(mini_cif, "2 water   .", "? water   .")
    assert session.load()
    v = Views(session)
    link = next(l.id for l in session.dic.children_of("entity")
                if l.child == "atom_site")
    res = v.children("entity", 'k:["?"]', link)
    assert res["count"] == 0 and res["rows"] == []
    # negative offset is clamped to 0
    ok = v.children("entity", 'k:["1"]', link, offset=-5, limit=2)
    assert ok["offset"] == 0 and len(ok["rows"]) == 2
    # wrong parent table for link raises NotFound
    with pytest.raises(NotFound):
        v.children("struct_asym", 'k:["A"]', link)


def test_malformed_refs_do_not_crash(session):
    v = Views(session)
    t = v.table("struct_asym")
    for bad in ("i:abc", "k:notjson", "k:123", "k:[1]", "k-1:[\"A\"]",
                "kabc:[\"A\"]", "bogus", None):
        assert v.resolve(t, bad) is None
    out = v.resolve_refs([{"cat": "struct_asym", "ref": "k:123"},
                          {"cat": "struct_asym", "ref": "k:[1]"},
                          "not-a-dict"])
    assert out == [None, None, None]


def test_http(session):
    httpd = make_server(session, port=0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        def get(path):
            with urllib.request.urlopen("http://127.0.0.1:%d%s"
                                        % (port, path)) as r:
                return r.read()

        def post(path, raw):
            req = urllib.request.Request(
                "http://127.0.0.1:%d%s" % (port, path), data=raw,
                method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req) as r:
                return r.read()

        assert b"CIF Explorer" in get("/")
        assert b"renderTrace" in get("/static/app.js")
        st = json.loads(get("/api/status"))
        assert st["state"] == "ready" and st["version"] == 1
        page = json.loads(get("/api/table?cat=atom_site&f=label_comp_id:GLY"))
        assert page["total"] == 1
        # malformed/blank around should not 500
        page = json.loads(get("/api/table?cat=atom_site&around="))
        assert page["offset"] == 0
        page = json.loads(get("/api/table?cat=atom_site&around=abc"))
        assert page["offset"] == 0
        row = json.loads(get("/api/row?cat=struct_asym&ref=k%3A%5B%22A%22%5D"))
        assert row["idx"] == 0
        with pytest.raises(urllib.error.HTTPError) as exc:
            get("/api/row?cat=struct_asym&ref=k:123")
        assert exc.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as exc:
            get("/api/table?cat=nope")
        assert exc.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as exc:
            get("/static//etc/passwd")
        assert exc.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as exc:
            post("/api/resolve", b"{bad json")
        assert exc.value.code == 400
        with pytest.raises(urllib.error.HTTPError) as exc:
            post("/api/resolve", b"[]")
        assert exc.value.code == 400
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_background_checks_and_watcher(mini_cif, mini_dic):
    import time
    from cif_explorer.server import Watcher
    s = Session(mini_cif, mini_dic)
    assert s.load(background_checks=True)
    deadline = time.time() + 5
    while s.issues is None and time.time() < deadline:
        time.sleep(0.02)
    assert s.issues == []
    w = Watcher(s, interval=0.05)
    w.start()
    try:
        edit(mini_cif, "_entry.id MINI", "_entry.id MINI2")
        deadline = time.time() + 5
        while s.version < 2 and time.time() < deadline:
            time.sleep(0.05)
        assert s.version == 2
        assert Views(s).table("entry").row(0) == ["MINI2"]
    finally:
        w.stop_event.set()
        w.join(timeout=2)
