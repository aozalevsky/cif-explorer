from conftest import edit

from cif_explorer import store
from cif_explorer.checks import Checker, run_ihm_validator


def _kinds(issues):
    return sorted((i.kind, i.category, tuple(i.items)) for i in issues)


def test_clean_file(mini_cif, mini_dic):
    issues = Checker(mini_dic).run(store.load(mini_cif))
    assert issues == []


def test_bad_values(mini_cif, mini_dic):
    edit(mini_cif, "2 water   .", "2 watr   .")              # enumeration
    edit(mini_cif, "3 CA GLY 2 A 1 3.250 0.5",
         "3 CA GLY x2 A 1 3.250 1.5")                            # int, range
    data = store.load(mini_cif)
    issues = Checker(mini_dic).run(data)
    by = {(i.category, tuple(i.items), i.message.split(" (")[0]): i
          for i in issues}
    enum = by[("entity", ("type",), "not an allowed value")]
    assert enum.rows == [1] and enum.values == ["watr"]
    bad_int = by[("atom_site", ("label_seq_id",), "does not match type 'int'")]
    assert bad_int.rows == [2]
    rng = by[("atom_site", ("occupancy",), "out of allowed range")]
    assert rng.severity == "warning" and rng.rows == [2]


def test_structure_problems(mini_cif, mini_dic):
    edit(mini_cif, "_entity.pdbx_description\n",
         "_entity.pdbx_description\n_entity.bogus\n")
    edit(mini_cif, "'Test protein'\n", "'Test protein' x\n")
    edit(mini_cif, "2 water   .", "2 water   . y\n#\n_other.thing 1\n")
    edit(mini_cif, "B 2 ?", "A 2 ?")                             # duplicate
    data = store.load(mini_cif)
    kinds = _kinds(Checker(mini_dic).run(data))
    assert ("unknown_item", "entity", ("bogus",)) in kinds
    assert ("unknown_category", "other", ()) in kinds
    assert ("duplicate_key", "struct_asym", ("id",)) in kinds


def test_missing_mandatory(mini_cif, mini_dic):
    edit(mini_cif, "_entry.id MINI\n", "")
    edit(mini_cif, "_struct_asym.entity_id\n", "")
    edit(mini_cif, "A 1 'chain A'\nB 2 ?", "A 'chain A'\nB ?")
    kinds = _kinds(Checker(mini_dic).run(store.load(mini_cif)))
    assert ("missing_category", "entry", ()) in kinds
    assert ("missing_item", "struct_asym", ("entity_id",)) in kinds


def test_broken_composite_link(mini_cif, mini_dic):
    # residue 3 of entity 1 is ALA; a GLY there does not exist
    edit(mini_cif, "4 CA ALA 3 A 1 -4.0 0", "4 CA GLY 3 A 1 -4.0 0")
    edit(mini_cif, "1 1 1 3 ALA ALA", "1 1 1 9 ALA ALA")
    issues = Checker(mini_dic).run(store.load(mini_cif))
    broken = {(i.category, i.items[0]): i for i in issues
              if i.kind == "broken_link"}
    assert broken[("atom_site", "label_seq_id")].rows == [3]
    assert broken[("seg", "seq_id_end")].rows == [0]
    assert ("seg", "seq_id_begin") not in broken


def test_cell_check(mini_dic):
    c = Checker(mini_dic)
    assert c.check_value("atom_site", "occupancy", "0") is None
    assert c.check_value("atom_site", "occupancy", "1.0") is None
    assert c.check_value("atom_site", "occupancy", "0.3") is None
    assert "range" in c.check_value("atom_site", "occupancy", "1.01")
    assert c.check_value("atom_site", "occupancy", "?") is None
    assert "mandatory" in c.check_value("entity", "id", "?")
    assert c.check_value("entity_poly_seq", "num", "1") is None
    assert "range" in c.check_value("entity_poly_seq", "num", "0")
    # ucode enumerations are case-insensitive, code ones are not
    assert c.check_value("entity", "type", "polymer") is None


def test_python_ihm_validator(mini_cif, mini_dic):
    data = store.load(mini_cif)
    assert run_ihm_validator(data.path, data.format, data.gzipped,
                             mini_dic) == []
    edit(mini_cif, "2 water   .", "2 watr   .")
    msgs = run_ihm_validator(data.path, data.format, data.gzipped, mini_dic)
    assert any("watr" in m for m in msgs)


def test_dense_columns(tmp_path, monkeypatch, mini_dic):
    monkeypatch.setattr(store, "BATCH", 2000)
    path = tmp_path / "big.cif"
    with open(path, "w") as fh:
        fh.write("data_X\nloop_\n_atom_site.id\n_atom_site.Cartn_x\n"
                 "_atom_site.occupancy\n")
        for i in range(5000):
            ident = {10: "'a b'", 20: "5"}.get(i, str(i + 1))
            x = {30: "1.2.3", 40: "nan"}.get(i, "%.3f" % (i * 0.25))
            occ = "1.5" if i == 50 else "1.0"
            fh.write("%s %s %s\n" % (ident, x, occ))
    data = store.load(str(path))
    assert type(data["atom_site"].columns["id"]).__name__ == "DenseColumn"
    issues = Checker(mini_dic).run(data)
    got = {(i.kind, i.items[0]): i for i in issues if i.items}
    assert got[("bad_value", "id")].rows == [10]
    assert got[("duplicate_key", "id")].rows == [4, 20]
    assert got[("bad_value", "cartn_x")].rows == [30, 40]
    assert got[("bad_value", "occupancy")].rows == [50]


def test_dense_fast_path_edge_cases(tmp_path, monkeypatch, mini_dic):
    # Ensure dense columns catch:
    # - embedded newlines in 'code' (previously bypassed by '$' in _charset_lut)
    # - leading '+' in PDBx 'float'
    # - bad float past row 100,000 (previously truncated)
    # - out-of-range value in a column that also contains an unparseable float
    monkeypatch.setattr(store, "BATCH", 2000)
    path = tmp_path / "edge.cif"
    n = 102000
    with open(path, "w") as fh:
        fh.write("data_X\nloop_\n_atom_site.id\n_atom_site.Cartn_x\n"
                 "_atom_site.occupancy\n")
        for i in range(n):
            if i == 10:
                ident = "\n;a\nb\n;\n"
            else:
                ident = str(i + 1)
            x = ("+1.5" if i == 20 else
                 "1.2.3" if i == 101500 else "%.3f" % (i * 0.001))
            occ = ("1.2.3" if i == 5 else
                   "2.5" if i == 101000 else "%.6f" % ((i % 100000) * 1e-6))
            fh.write("%s %s %s\n" % (ident, x, occ))
    data = store.load(str(path))
    t = data["atom_site"]
    assert all(type(c).__name__ == "DenseColumn" for c in t.columns.values())
    issues = Checker(mini_dic).check_table(t)
    by = {(i.items[0], i.message.split(" (")[0]): i
          for i in issues if i.kind == "bad_value"}
    assert by[("id", "does not match type 'code'")].rows == [10]
    assert by[("cartn_x", "does not match type 'float'")].rows == [20, 101500]
    assert by[("occupancy", "does not match type 'float'")].rows == [5]
    assert by[("occupancy", "out of allowed range")].rows == [101000]


def test_dense_positive_int_and_mixed_radix_overflow(tmp_path):
    import numpy as np
    from cif_explorer.checks import _DenseKeySet, _mixed_radix
    from cif_explorer.store import DenseColumn, Table

    # Build a minimal dict with positive_int
    dic_path = tmp_path / "p.dic"
    dic_path.write_text(
        "data_p\n_dictionary.title p\n_dictionary.version 1\n"
        "save_t\n_category.id t\n_category.mandatory_code no\n"
        "_category_key.name '_t.n'\nsave_\n"
        "save__t.n\n_item.name '_t.n'\n_item.category_id t\n"
        "_item.mandatory_code yes\n_item_type.code positive_int\nsave_\n"
        "loop_\n_item_type_list.code\n_item_type_list.primitive_code\n"
        "_item_type_list.construct\npositive_int numb '[1-9][0-9]*'\n"
    )
    from cif_explorer.dictionary import DictInfo
    dic = DictInfo([str(dic_path)])
    arr = np.array(["-5", "0", "+1", "01"] + [str(i) for i in range(1, 2000)],
                   dtype="S")
    t = Table("t", ["n"], [DenseColumn(arr)])
    issues = Checker(dic).check_table(t)
    bad = [i for i in issues if i.kind == "bad_value"]
    assert len(bad) == 1 and bad[0].rows == [0, 1, 2, 3]

    # _mixed_radix fallback when product of sizes >= 2**62
    c0 = np.array([0, 1, 0, 2], dtype=np.int64)
    c1 = np.array([5, 6, 5, 7], dtype=np.int64)
    key = _mixed_radix([c0, c1], [1 << 40, 1 << 40])
    uniq, counts = np.unique(key, return_counts=True)
    assert len(uniq) == 3 and sorted(counts.tolist()) == [1, 1, 2]

    # _DenseKeySet membership including longer-than-itemsize & non-ASCII keys
    ks = _DenseKeySet(np.array(["1", "22", "333"], dtype="S"))
    assert ("1",) in ks and ("333",) in ks
    assert ("3334",) not in ks and ("café",) not in ks and ("2",) not in ks
