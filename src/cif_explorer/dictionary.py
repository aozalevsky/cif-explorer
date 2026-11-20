"""PDBx + IHM dictionary access.

Parsing is done by python-ihm: :func:`ihm.dictionary.read` gives types,
enumerations and mandatory flags (used for validation), and a second
:class:`ihm.format.CifReader` pass with a few extra handlers collects what
:class:`ihm.dictionary.Dictionary` does not keep: item descriptions,
category keys and groups, ranges, units, every parent/child link (an item
can have several parents) and the raw type codes.
"""
from __future__ import annotations

import os
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import ihm.dictionary
import ihm.format

DICT_URL = "https://mmcif.wwpdb.org/dictionaries/ascii/"
DICT_FILES = ("mmcif_pdbx_v50.dic", "mmcif_ihm_ext.dic")
_SENTINELS = {"mmcif_pdbx_v50.dic": b"save_atom_site",
              "mmcif_ihm_ext.dic": b"save_ihm_dataset_list"}


def cache_dir() -> str:
    env = os.environ.get("CIF_EXPLORER_CACHE")
    if env:
        return env
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache")
    return os.path.join(base, "cif_explorer")


def _download(name: str, dest: str) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = "%s.%d.part" % (dest, os.getpid())
    req = urllib.request.Request(DICT_URL + name,
                                 headers={"User-Agent": "cif-explorer"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, \
                open(part, "wb") as fh:
            shutil.copyfileobj(resp, fh)
        with open(part, "rb") as fh:
            if _SENTINELS[name] not in fh.read():
                raise OSError("downloaded %s does not look like the "
                              "dictionary" % name)
        os.replace(part, dest)
    except BaseException:
        if os.path.exists(part):
            try:
                os.unlink(part)
            except OSError:
                pass
        raise


def locate_dictionaries(update: bool = False) -> Tuple[List[str], List[str]]:
    """Return (paths, messages) for the PDBx and IHM dictionaries, downloading
    them into the cache directory when missing (or when ``update``)."""
    paths, messages = [], []
    ddir = os.path.join(cache_dir(), "dictionaries")
    for name in DICT_FILES:
        dest = os.path.join(ddir, name)
        if update or not os.path.exists(dest):
            try:
                _download(name, dest)
                messages.append("downloaded %s" % name)
            except (urllib.error.URLError, OSError) as exc:
                messages.append("could not download %s: %s" % (name, exc))
        if os.path.exists(dest):
            paths.append(dest)
    return paths, messages


# --------------------------------------------------------------------------
# Extra metadata collected with python-ihm handlers
# --------------------------------------------------------------------------

@dataclass
class ItemMeta:
    name: str                       # e.g. "_atom_site.Cartn_x" (dict case)
    category: str                   # lowercase, no leading '_'
    keyword: str                    # keyword as written in the dictionary
    description: str = ""
    type_code: Optional[str] = None
    mandatory: bool = False
    units: Optional[str] = None
    ranges: List[Tuple[Optional[str], Optional[str]]] = field(
        default_factory=list)
    enum: List[Tuple[str, Optional[str]]] = field(default_factory=list)


@dataclass
class CategoryMeta:
    name: str
    description: str = ""
    mandatory: bool = False
    keys: List[str] = field(default_factory=list)     # lowercase keywords
    groups: List[str] = field(default_factory=list)


class _Meta:
    """Accumulates save-frame contents while python-ihm reads a dictionary."""

    FRAME_CATEGORIES = {
        "_category": ["id", "description", "mandatory_code"],
        "_category_key": ["name"],
        "_category_group": ["id"],
        "_item": ["name", "category_id", "mandatory_code"],
        "_item_description": ["description"],
        "_item_type": ["code"],
        "_item_range": ["minimum", "maximum"],
        "_item_units": ["code"],
        "_item_linked": ["child_name", "parent_name"],
        "_item_enumeration": ["value", "detail"],
    }
    TOP_CATEGORIES = {
        "_item_type_list": ["code", "primitive_code", "construct"],
        "_dictionary": ["title", "version"],
    }

    def __init__(self):
        self.frame: Dict[str, list] = {}
        self.items: Dict[str, ItemMeta] = {}
        self.categories: Dict[str, CategoryMeta] = {}
        self.links: Set[Tuple[str, str]] = set()
        self.types: Dict[str, Tuple[str, str]] = {}
        self.versions: List[Tuple[str, str]] = []

    def handlers(self):
        hs = {}
        for cat, keys in self.FRAME_CATEGORIES.items():
            hs[cat] = _FrameHandler(self, cat, keys, top=False)
        for cat, keys in self.TOP_CATEGORIES.items():
            hs[cat] = _FrameHandler(self, cat, keys, top=True)
        return hs

    def top(self, category, row):
        if category == "_item_type_list":
            code, prim, construct = row
            if code:
                self.types[code] = (prim or "char", (construct or "").strip())
        elif category == "_dictionary":
            title, version = row
            if title:
                self.versions.append((title, version or ""))

    def end_frame(self):
        f, self.frame = self.frame, {}
        if not f:
            return
        for child, parent in f.get("_item_linked", []):
            if child and parent:
                self.links.add((child.lower(), parent.lower()))
        if "_item" in f:
            desc = next((r[0] for r in f.get("_item_description", [])
                         if r[0]), "")
            tcode = next((r[0] for r in f.get("_item_type", []) if r[0]),
                         None)
            units = next((r[0] for r in f.get("_item_units", []) if r[0]),
                         None)
            ranges = [(lo, hi) for lo, hi in f.get("_item_range", [])]
            enum = [(v, d) for v, d in f.get("_item_enumeration", [])
                    if v is not None]
            for name, cat_id, mand in f["_item"]:
                if not name or "." not in name:
                    continue
                cat, kw = name.split(".", 1)
                cat = (cat_id or cat.lstrip("_")).lower()
                key = "%s.%s" % (cat, kw.lower())
                old = self.items.get(key)
                im = ItemMeta(name=name, category=cat, keyword=kw,
                              description=(desc or (old.description
                                                    if old else "")),
                              type_code=tcode or (old.type_code if old
                                                  else None),
                              mandatory=(mand or "").lower() == "yes",
                              units=units or (old.units if old else None),
                              ranges=ranges or (old.ranges if old else []),
                              enum=enum or (old.enum if old else []))
                self.items[key] = im
        elif "_category" in f:
            cid, desc, mand = f["_category"][0]
            if cid:
                cm = CategoryMeta(
                    name=cid.lower(), description=(desc or "").strip(),
                    mandatory=(mand or "").lower() == "yes",
                    keys=[r[0].split(".", 1)[1].lower()
                          for r in f.get("_category_key", [])
                          if r[0] and "." in r[0]],
                    groups=[r[0] for r in f.get("_category_group", [])
                            if r[0]])
                self.categories[cm.name] = cm


class _FrameHandler:
    not_in_file = omitted = unknown = None
    _int_keys = _float_keys = _bool_keys = frozenset()

    def __init__(self, meta: _Meta, category: str, keys: List[str],
                 top: bool):
        self.meta, self.category, self._keys, self.top = (meta, category,
                                                          keys, top)

    def __call__(self, *args):
        if self.top:
            self.meta.top(self.category, args)
        else:
            self.meta.frame.setdefault(self.category, []).append(args)

    def end_save_frame(self):
        self.meta.end_frame()


# --------------------------------------------------------------------------
# Composite links
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Link:
    """A (possibly composite) parent/child relationship between categories.

    ``child_items[i]`` in ``child`` refers to ``parent_items[i]`` in
    ``parent``.  The first ``n_direct`` pairs come straight from the
    dictionary's ``_item_linked``; the rest were inferred so that composite
    parent keys (e.g. entity_poly_seq: entity_id + num + mon_id) are matched
    completely.
    """
    child: str
    parent: str
    child_items: Tuple[str, ...]
    parent_items: Tuple[str, ...]
    n_direct: int

    @property
    def id(self) -> str:
        return "%s(%s)>%s(%s)" % (self.child, ",".join(self.child_items),
                                  self.parent, ",".join(self.parent_items))

    def describe(self) -> str:
        return ", ".join("%s=%s" % (c, p) if c != p else c for c, p in
                         zip(self.child_items, self.parent_items))


def _common_suffix(a: str, b: str) -> int:
    n = 0
    for x, y in zip(reversed(a), reversed(b)):
        if x != y:
            break
        n += 1
    return n


def _common_prefix(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _similarity(a: str, b: str) -> Tuple[int, int]:
    return (_common_suffix(a, b), _common_prefix(a, b))


def build_links(pairs: Iterable[Tuple[str, str]],
                category_keys: Dict[str, Sequence[str]]) -> List[Link]:
    """Group item-level links ('_cat.item' child -> parent) into category
    level links, completing composite parent keys where possible."""
    def split(full):
        cat, item = full.split(".", 1)
        return cat.lstrip("_"), item

    parents_of: Dict[str, Set[str]] = {}
    groups: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
    items_of: Dict[str, Set[str]] = {}
    for child, parent in pairs:
        if "." not in child or "." not in parent:
            continue
        cc, ci = split(child)
        pc, pi = split(parent)
        parents_of.setdefault("%s.%s" % (cc, ci), set()).add(
            "%s.%s" % (pc, pi))
        groups.setdefault((cc, pc), []).append((ci, pi))
        items_of.setdefault(cc, set()).add(ci)

    root_cache: Dict[str, frozenset] = {}

    def _roots(item: str, seen=()) -> Tuple[frozenset, bool]:
        if item in root_cache:
            return root_cache[item], False
        if item in seen:
            return frozenset([item]), True
        ps = parents_of.get(item)
        if not ps:
            r = frozenset([item])
            root_cache[item] = r
            return r, False
        parts = []
        cycled = False
        for p in ps:
            pr, pc = _roots(p, seen + (item,))
            parts.append(pr)
            cycled = cycled or pc
        r = frozenset().union(*parts)
        if not cycled:
            root_cache[item] = r
        return r, cycled

    def roots(item: str) -> frozenset:
        return _roots(item)[0]

    links: List[Link] = []
    for (cc, pc), plist in sorted(groups.items()):
        keys = [k for k in category_keys.get(pc, [])]
        plist = sorted(set(plist), key=lambda x: (
            keys.index(x[1]) if x[1] in keys else len(keys), x[0]))
        composite = len(keys) > 1
        roles: List[Dict[str, str]] = []      # parent item -> child item
        anchors: List[str] = []
        if not composite:
            for ci, pi in plist:
                roles.append({pi: ci})
                anchors.append(ci)
        else:
            by_parent: Dict[str, List[str]] = {}
            for ci, pi in plist:
                by_parent.setdefault(pi, []).append(ci)
            # the parent item referenced by most child items defines the
            # roles (e.g. num <- seq_id_begin / seq_id_end)
            anchor_pi = max(by_parent, key=lambda p: (
                len(by_parent[p]), -(keys.index(p) if p in keys
                                     else len(keys))))
            anchors = list(by_parent[anchor_pi])
            roles = [{anchor_pi: ci} for ci in anchors]
            for pi, cis in by_parent.items():
                if pi == anchor_pi:
                    continue
                if len(cis) == 1:
                    for role in roles:          # shared, e.g. entity_id
                        role[pi] = cis[0]
                    continue
                scored = sorted(((_similarity(ci, anchors[r]), ci, r)
                                 for ci in cis for r in range(len(roles))),
                                reverse=True)
                used_c, used_r = set(), set()
                for _, ci, r in scored:
                    if ci in used_c or r in used_r:
                        continue
                    roles[r][pi] = ci
                    used_c.add(ci)
                    used_r.add(r)
                for ci in cis:                  # more children than roles
                    if ci not in used_c:
                        roles.append({pi: ci})
                        anchors.append(ci)
        direct = [dict(r) for r in roles]
        # complete composite keys via shared root items
        if composite:
            for r, role in enumerate(roles):
                for k in keys:
                    if k in role:
                        continue
                    kroots = roots("%s.%s" % (pc, k))
                    used = set(role.values())
                    cands = [ci for ci in items_of.get(cc, ())
                             if ci not in used
                             and roots("%s.%s" % (cc, ci)) & kroots]
                    if not cands:
                        continue
                    scored = sorted(((_similarity(ci, anchors[r]), ci)
                                     for ci in cands), reverse=True)
                    if len(scored) > 1 and scored[0][0] == scored[1][0]:
                        continue  # ambiguous
                    role[k] = scored[0][1]
        seen = set()
        for role, d in zip(roles, direct):
            order = [k for k in keys if k in role] + sorted(
                k for k in role if k not in keys)
            # direct pairs first
            order = [k for k in order if k in d] + [k for k in order
                                                    if k not in d]
            link = Link(child=cc, parent=pc,
                        child_items=tuple(role[k] for k in order),
                        parent_items=tuple(order), n_direct=len(d))
            if link.id not in seen:
                seen.add(link.id)
                links.append(link)
    return links


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

class DictInfo:
    """Merged dictionary data used by the explorer."""

    def __init__(self, paths: Sequence[str]):
        self.paths = list(paths)
        self.ihm: Optional[ihm.dictionary.Dictionary] = None
        meta = _Meta()
        for path in self.paths:
            with open(path, encoding="utf-8", errors="replace") as fh:
                d = ihm.dictionary.read(fh)
            self.ihm = d if self.ihm is None else self.ihm + d
            with open(path, encoding="utf-8", errors="replace") as fh:
                r = ihm.format.CifReader(fh, meta.handlers())
                r.read_file()
            meta.end_frame()
        self.items = meta.items
        self.categories = meta.categories
        self.types = meta.types
        self.versions = meta.versions
        self._fix_item_types()
        # python-ihm's Dictionary knows categories that only appear through
        # extension items (e.g. atom_site.ihm_model_id); keep those too
        if self.ihm is not None:
            for name, cat in self.ihm.categories.items():
                if name not in self.categories:
                    self.categories[name] = CategoryMeta(
                        name=name, description=(cat.description or ""),
                        mandatory=bool(cat.mandatory))
        self.links = build_links(
            meta.links, {c: m.keys for c, m in self.categories.items()})
        self._parents: Dict[str, List[Link]] = {}
        self._children: Dict[str, List[Link]] = {}
        for link in self.links:
            self._parents.setdefault(link.child, []).append(link)
            self._children.setdefault(link.parent, []).append(link)

    def _fix_item_types(self):
        """Resolve type codes used by one dictionary but defined in another
        (e.g. the IHM dictionary uses PDBx's 'atcode')."""
        if self.ihm is None:
            return
        made: Dict[str, ihm.dictionary.ItemType] = {}
        for cname, cat in self.ihm.categories.items():
            for kname, kw in cat.keywords.items():
                if kw.item_type is not None:
                    continue
                im = self.items.get("%s.%s" % (cname, kname))
                if im is None or im.type_code not in self.types:
                    continue
                if im.type_code not in made:
                    prim, construct = self.types[im.type_code]
                    made[im.type_code] = ihm.dictionary.ItemType(
                        im.type_code, prim, construct)
                kw.item_type = made[im.type_code]
                if kw.enumeration:
                    kw.enumeration.case_sensitive = \
                        kw.item_type.case_sensitive

    # -- lookups ----------------------------------------------------------
    def category(self, name: str) -> Optional[CategoryMeta]:
        return self.categories.get(name.lower())

    def item(self, cat: str, item: str) -> Optional[ItemMeta]:
        return self.items.get("%s.%s" % (cat.lower(), item.lower()))

    def keyword(self, cat: str, item: str):
        """python-ihm Keyword (mandatory, enumeration, item_type) or None."""
        if self.ihm is None:
            return None
        c = self.ihm.categories.get(cat.lower())
        if c is None:
            return None
        return c.keywords.get(item.lower())

    def parents_of(self, cat: str) -> List[Link]:
        return self._parents.get(cat.lower(), [])

    def children_of(self, cat: str) -> List[Link]:
        return self._children.get(cat.lower(), [])

    def display_name(self, cat: str, item: str) -> str:
        im = self.item(cat, item)
        return im.keyword if im else item

    @property
    def version_string(self) -> str:
        return ", ".join("%s %s" % (t, v) for t, v in self.versions)


def load_dictionary(paths: Optional[Sequence[str]] = None,
                    update: bool = False) -> Tuple[Optional[DictInfo],
                                                   List[str]]:
    """Load the dictionaries (explicit ``paths`` or cached/downloaded)."""
    messages: List[str] = []
    if not paths:
        paths, messages = locate_dictionaries(update=update)
    if not paths:
        messages.append("no dictionary available: validation and tracing "
                        "are disabled")
        return None, messages
    return DictInfo(paths), messages
