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


def test_http(session):
    httpd = make_server(session, port=0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        def get(path):
            with urllib.request.urlopen("http://127.0.0.1:%d%s"
                                        % (port, path)) as r:
                return r.read()
        assert b"CIF Explorer" in get("/")
        assert b"renderTrace" in get("/static/app.js")
        st = json.loads(get("/api/status"))
        assert st["state"] == "ready" and st["version"] == 1
        page = json.loads(get("/api/table?cat=atom_site&f=label_comp_id:GLY"))
        assert page["total"] == 1
        row = json.loads(get("/api/row?cat=struct_asym&ref=k%3A%5B%22A%22%5D"))
        assert row["idx"] == 0
        with pytest.raises(urllib.error.HTTPError) as exc:
            get("/api/table?cat=nope")
        assert exc.value.code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
