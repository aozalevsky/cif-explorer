import gzip
import shutil

import ihm.format
import numpy as np
import pytest

from cif_explorer import store
from cif_explorer.store import CatColumn, DenseColumn, ListColumn


def test_load_small(mini_cif):
    data = store.load(mini_cif)
    assert list(data.tables)[:3] == ["entry", "entity", "chem_comp"]
    ent = data["entity"]
    assert ent.nrows == 2
    assert ent.items == ["id", "type", "pdbx_description"]
    assert ent.row(0) == ["1", "polymer", "Test protein"]
    assert ent.row(1) == ["2", "water", "."]
    # text field and '?'
    cc = data["chem_comp"]
    assert cc.get(2, "name") == "water, with a\nmulti-line name"
    assert data["struct_asym"].get(1, "details") == "?"
    # key-value category
    assert data["entry"].row(0) == ["MINI"]
    # line numbers of categories come from python-ihm
    assert data["entity"].line == 6
    assert not data.warnings


def test_case_insensitive_lookup(mini_cif):
    data = store.load(mini_cif)
    a = data["atom_site"]
    assert a.display[6] == "Cartn_x"
    assert a.items[6] == "cartn_x"
    assert a.has("CARTN_X")
    assert a.get(0, "Cartn_x") == "1.000"


def test_find(mini_cif):
    data = store.load(mini_cif)
    a = data["atom_site"]
    assert a.find([("label_comp_id", "ALA")]).tolist() == [0, 1, 3]
    assert a.find([("label_comp_id", "ALA"), ("label_seq_id", "1")]).tolist() \
        == [0, 1]
    assert a.find([("label_comp_id", "XXX")]).tolist() == []
    assert a.find([("no_such_item", "1")]).tolist() == []
    assert a.find([]).tolist() == list(range(5))


def test_gzip(mini_cif, tmp_path):
    gz = tmp_path / "mini.cif.gz"
    with open(mini_cif, "rb") as src, gzip.open(gz, "wb") as dst:
        shutil.copyfileobj(src, dst)
    data = store.load(str(gz))
    assert data.gzipped
    assert data["atom_site"].nrows == 5


def _write_big(path, n):
    with open(path, "w") as fh:
        fh.write("data_BIG\nloop_\n_atom_site.id\n_atom_site.label_comp_id\n"
                 "_atom_site.Cartn_x\n_atom_site.label_asym_id\n")
        for i in range(n):
            fh.write("%d %s %.3f %s\n" % (i + 1, "ALA" if i % 3 else "GLY",
                                          i * 0.5, "AB"[i % 2]))


def test_packing(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "BATCH", 2000)
    path = tmp_path / "big.cif"
    _write_big(path, 5500)
    data = store.load(str(path))
    t = data["atom_site"]
    assert t.packed and t.nrows == 5500
    assert isinstance(t.columns["id"], DenseColumn)
    assert isinstance(t.columns["cartn_x"], DenseColumn)
    assert isinstance(t.columns["label_comp_id"], CatColumn)
    assert t.columns["label_comp_id"].codes.dtype == np.uint8
    assert t.row(0) == ["1", "GLY", "0.000", "A"]
    assert t.row(5499) == ["5500", "GLY", "2749.500", "B"]
    # rows spanning a batch boundary
    assert t.rows(1999, 2001) == [["2000", "ALA", "999.500", "B"],
                                  ["2001", "ALA", "1000.000", "A"]]
    assert t.take([4, 2]) == [t.row(4), t.row(2)]
    assert len(t.find([("label_comp_id", "GLY")])) == 1834
    assert t.find([("id", "4321")]).tolist() == [4320]
    uniq, codes = t.columns["label_asym_id"].factorize()
    assert sorted(uniq) == ["A", "B"] and len(codes) == 5500


def test_list_column_factorize():
    c = ListColumn(["b", "a", "b"])
    uniq, codes = c.factorize()
    assert [uniq[i] for i in codes] == ["b", "a", "b"]
    assert c.rows_eq("b").tolist() == [0, 2]


def test_parse_error(tmp_path):
    path = tmp_path / "bad.cif"
    path.write_text("data_X\n_entry.id\n;unterminated\n")
    with pytest.raises(ihm.format.CifParserError):
        store.load(str(path))
