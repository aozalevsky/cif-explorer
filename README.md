# cif-explorer

A local, browser-based explorer for IHMCIF / mmCIF files, built for
PDB-IHM biocuration. Pick any row in any category and trace it **up**
(the rows it refers to) and **down** (the rows that refer to it) through the
whole hierarchy, with dictionary checks shown next to the values.

```
cif_explorer 9A1N.cif          # opens http://localhost:<port>/ in the browser
```

## Features

- **Trace tree.** Click a row; the right panel shows its fields, its parent
  rows and the child rows in every category that refers to it. Expand any node
  to keep walking up or down. "↑ all parents" opens the full chain of
  ancestors (e.g. `atom_site → struct_asym → entity`, `ihm_model_list →
  ihm_modeling_protocol → ihm_dataset_group …`).
- **Composite links.** Parent/child relationships come from the PDBx and IHM
  dictionaries (`_item_linked`). Composite parent keys are completed, so
  `atom_site` rows link to `entity_poly_seq` by entity + residue number +
  residue name, and cross-link partners `_1` / `_2` are traced separately.
- **Dictionary checks.** Every value is checked against the item's type,
  enumeration, range and mandatory flag (python-ihm's dictionary objects);
  missing mandatory items, unknown categories/items, duplicate keys and broken
  links (child values with no parent row) are listed in the checks panel, with
  links to the offending rows. Bad cells are highlighted in the table and the
  trace. python-ihm's own `Dictionary.validate` can be run from the same panel.
- **Live reload.** The file is watched; when it changes it is re-read and the
  view (category, page, filters, selected row, expanded trace nodes, scroll
  positions) is kept. If the new version does not parse, the error is shown
  and the previous version stays on screen.
- **Big files.** Reading is done by python-ihm's C tokenizer. Large tables
  (`atom_site`, `ihm_starting_model_coord`, `ihm_sphere_obj_site`, …) are packed
  into numpy arrays while reading: a 143 MB entry with 1.9 M atoms needs about
  100 MB of memory and loads in under 10 s. Paging, filtering and link lookups
  work on the packed columns.
- Reads `.cif`, `.cif.gz` and BinaryCIF (`.bcif`).

## Install

```
pip install cif-explorer        # once released
pip install -e .                # from a checkout
```

## Usage

```
cif_explorer FILE [--port N] [--host ADDR] [--no-browser] [--no-watch]
                  [--dict DIC ...] [--update-dicts]
```

The PDBx (`mmcif_pdbx_v50.dic`) and IHM (`mmcif_ihm_ext.dic`) dictionaries are
downloaded from mmcif.wwpdb.org on first use and cached in
`~/.cache/cif_explorer/dictionaries` (override the location with
`CIF_EXPLORER_CACHE`, or pass local files with `--dict`). Use
`--update-dicts` to refresh them.

In the browser:

| action | how |
| --- | --- |
| trace a row | click it in the table |
| filter by a value | double-click the cell |
| full-text filter in a category | "contains…" box in the toolbar |
| search all categories | search box at the top (`/`) |
| dictionary definition | hover a column header or field name |
| jump from the tree to the table | `⌖ table` on a node |
| re-root the tree | `⤒ root` on a node |

## Development

```
uv venv && uv pip install -e ".[test]"
pytest
```

All CIF parsing is delegated to [python-ihm](https://github.com/ihmwg/python-ihm);
this package contains no tokenizer of its own.
