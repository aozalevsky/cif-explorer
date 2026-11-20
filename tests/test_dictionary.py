from cif_explorer.dictionary import build_links


def _links(dic, child, parent):
    return [(l.child_items, l.parent_items, l.n_direct)
            for l in dic.parents_of(child) if l.parent == parent]


def test_metadata(mini_dic):
    assert mini_dic.version_string == "mini.dic 0.1"
    cm = mini_dic.category("entity_poly_seq")
    assert cm.keys == ["entity_id", "num", "mon_id"]
    assert cm.groups == ["inclusive_group", "mini_group"]
    im = mini_dic.item("atom_site", "cartn_x")
    assert im.keyword == "Cartn_x" and im.units == "angstroms"
    assert im.type_code == "float"
    assert "Second line" in im.description
    assert [v for v, _ in mini_dic.item("entity", "type").enum] == [
        "polymer", "non-polymer", "water"]
    kw = mini_dic.keyword("entity", "id")
    assert kw.mandatory and kw.item_type.name == "code"


def test_composite_link(mini_dic):
    # atom_site.label_seq_id -> entity_poly_seq.num is completed with the
    # entity and residue name, which share their root parents
    assert _links(mini_dic, "atom_site", "entity_poly_seq") == [
        (("label_seq_id", "label_entity_id", "label_comp_id"),
         ("num", "entity_id", "mon_id"), 1)]


def test_begin_end_roles(mini_dic):
    got = sorted(_links(mini_dic, "seg", "entity_poly_seq"))
    assert [g[0] for g in got] == [
        ("seq_id_begin", "entity_id", "comp_id_begin"),
        ("seq_id_end", "entity_id", "comp_id_end")]


def test_pair_roles(mini_dic):
    got = sorted(_links(mini_dic, "xl", "entity_poly_seq"))
    assert [g[0] for g in got] == [
        ("seq_id_1", "entity_id_1", "comp_id_1"),
        ("seq_id_2", "entity_id_2", "comp_id_2")]


def test_children_index(mini_dic):
    kids = {l.child for l in mini_dic.children_of("entity")}
    assert {"struct_asym", "atom_site", "entity_poly_seq"} <= kids


def test_build_links_shared_component():
    # chem_comp_bond-like: two atoms of the same component
    pairs = [("_bond.atom_id_1", "_atom.atom_id"),
             ("_bond.atom_id_2", "_atom.atom_id"),
             ("_bond.comp_id", "_atom.comp_id")]
    links = build_links(pairs, {"atom": ["comp_id", "atom_id"]})
    assert sorted((l.child_items, l.parent_items) for l in links) == [
        (("comp_id", "atom_id_1"), ("comp_id", "atom_id")),
        (("comp_id", "atom_id_2"), ("comp_id", "atom_id"))]


def test_build_links_simple_key():
    pairs = [("_a.x_id", "_x.id"), ("_a.y_id", "_x.id")]
    links = build_links(pairs, {"x": ["id"]})
    assert sorted(l.child_items for l in links) == [("x_id",), ("y_id",)]
