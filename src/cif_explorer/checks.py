"""Dictionary conformance checks.

Two kinds of checks:

* :class:`Checker` - fast checks over the data already in memory, using the
  python-ihm dictionary objects (type regular expressions, enumerations,
  mandatory flags) plus ranges, duplicate keys and parent/child integrity.
  Every distinct value is checked once, so this is cheap even for atom_site,
  and every problem points at concrete rows.
* :func:`run_ihm_validator` - python-ihm's own
  :meth:`ihm.dictionary.Dictionary.validate`, run on demand over the file.
"""
from __future__ import annotations

import gzip
import io
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

import ihm.dictionary

from .dictionary import DictInfo, Link
from .store import NULLS, CifData, Column, DenseColumn, Table

MAX_EXAMPLES = 25

ERROR, WARNING, INFO = "error", "warning", "info"


@dataclass
class Issue:
    severity: str
    kind: str
    category: str
    message: str
    items: List[str] = field(default_factory=list)
    count: int = 0                       # affected rows
    rows: List[int] = field(default_factory=list)      # example rows
    values: List[str] = field(default_factory=list)    # example values
    link: Optional[str] = None

    def to_json(self) -> dict:
        return {"severity": self.severity, "kind": self.kind,
                "category": self.category, "items": self.items,
                "message": self.message, "count": self.count,
                "rows": self.rows, "values": self.values, "link": self.link}


def _num(s: Optional[str]) -> Optional[float]:
    if s is None or s in NULLS:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _in_ranges(x: float, ranges) -> bool:
    """DDL2 semantics: each (min, max) is an open interval, or the single
    value min when min == max; '.' means unbounded."""
    for lo, hi in ranges:
        if lo is not None and hi is not None and lo == hi:
            if x == lo:
                return True
            continue
        if (lo is None or x > lo) and (hi is None or x < hi):
            return True
    return False


# bytes allowed in plain decimal numbers (0 = numpy padding)
_FLOAT_LUT = np.zeros(256, dtype=bool)
for _c in b"0123456789.-+eE":
    _FLOAT_LUT[_c] = True
_FLOAT_LUT[0] = True
_INT_LUT = np.zeros(256, dtype=bool)
for _c in b"0123456789-+":
    _INT_LUT[_c] = True
_INT_LUT[0] = True


_CHARCLASS_STAR = re.compile(r"\[\]?[^\]]*\]\*")


def _charset_lut(item_type) -> Optional[np.ndarray]:
    """For types whose construct is one character class repeated
    ('[...]*', e.g. code, line, text) return a byte -> allowed table, so a
    whole numpy bytes column can be checked at once."""
    if item_type is None or not _CHARCLASS_STAR.fullmatch(
            item_type.construct):
        return None
    lut = np.zeros(256, dtype=bool)
    for b in range(1, 128):
        lut[b] = bool(item_type.regex.match(chr(b)))
    lut[0] = True        # numpy padding
    return lut


class Checker:
    def __init__(self, dic: Optional[DictInfo]):
        self.dic = dic
        self._luts: Dict[str, Optional[np.ndarray]] = {}
        self._cell_cache: Dict[Tuple[str, str, str], Optional[str]] = {}
        self._ranges: Dict[Tuple[str, str], list] = {}
        self._keysets: Dict[Tuple[int, str, Tuple[str, ...]], Set] = {}

    # -- single values ------------------------------------------------------
    def _parsed_ranges(self, cat: str, item: str):
        key = (cat, item)
        if key not in self._ranges:
            im = self.dic.item(cat, item) if self.dic else None
            rs = []
            if im is not None:
                for lo, hi in im.ranges:
                    rs.append((_num(lo), _num(hi)))
            self._ranges[key] = rs
        return self._ranges[key]

    def check_value(self, cat: str, item: str, value: str) -> Optional[str]:
        """Problem with one value, or None. Results are cached."""
        if self.dic is None:
            return None
        key = (cat, item, value)
        try:
            return self._cell_cache[key]
        except KeyError:
            pass
        msg = self._check_value(cat, item, value)
        if len(self._cell_cache) > 500000:
            self._cell_cache.clear()
        self._cell_cache[key] = msg
        return msg

    def _check_value(self, cat, item, value):
        kw = self.dic.keyword(cat, item)
        if kw is None:
            return None
        if value in NULLS:
            if value == "?" and kw.mandatory:
                return "mandatory item has value '?'"
            return None
        if kw.enumeration and value not in kw.enumeration:
            opts = sorted(kw.enumeration)
            shown = ", ".join(opts[:12]) + (", ..." if len(opts) > 12
                                            else "")
            return "not an allowed value (%s)" % shown
        it = kw.item_type
        if it is not None and not it.regex.match(value):
            return "does not match type '%s'" % it.name
        rs = self._parsed_ranges(cat, item)
        if rs:
            x = _num(value)
            if x is not None and not _in_ranges(x, rs):
                return "out of allowed range (%s)" % self.range_text(cat,
                                                                     item)
        return None

    def range_text(self, cat, item) -> str:
        im = self.dic.item(cat, item) if self.dic else None
        if im is None:
            return ""
        parts = []
        for lo, hi in im.ranges:
            if lo == hi and lo not in (None, "."):
                parts.append("= %s" % lo)
            else:
                parts.append("%s < x < %s" % (
                    lo if lo not in (None, ".") else "-inf",
                    hi if hi not in (None, ".") else "inf"))
        return " or ".join(parts)

    # -- whole columns ------------------------------------------------------
    def _numeric_fast_ok(self, col: Column, kw) -> Optional[np.ndarray]:
        """For a dense bytes column, return a boolean 'maybe bad' mask (rows
        needing an individual check), or None if no fast path applies."""
        if not isinstance(col, DenseColumn) or col.arr.dtype.kind != "S":
            return None
        it = kw.item_type
        if it is None or kw.enumeration:
            return None
        numeric = it.primitive_code == "numb"
        if numeric:
            lut = (_INT_LUT if it.name in ("int", "positive_int")
                   else _FLOAT_LUT)
        else:
            if it.name not in self._luts:
                self._luts[it.name] = _charset_lut(it)
            lut = self._luts[it.name]
            if lut is None:
                return None
        arr = col.arr
        width = arr.dtype.itemsize
        nulls = (arr == b"?") | (arr == b".")
        chars_ok = lut[arr.view(np.uint8).reshape(len(arr), width)].all(
            axis=1)
        maybe_bad = ~nulls & ~chars_ok
        if numeric:
            try:
                arr[~nulls & chars_ok].astype(np.float64)
            except ValueError:
                return ~nulls
        return maybe_bad

    def _column_problems(self, table: Table, item: str
                         ) -> Dict[str, Tuple[np.ndarray, List[str]]]:
        """message -> (rows, example values) for one column."""
        col = table.columns[item]
        cat = table.name
        kw = self.dic.keyword(cat, item)
        if kw is None:
            return {}
        problems: Dict[str, List] = {}
        mask = self._numeric_fast_ok(col, kw)
        if mask is not None:
            rows = np.flatnonzero(mask)
            # check the suspicious ones individually
            for r in rows[:100000].tolist():
                v = col.value(r)
                msg = self.check_value(cat, item, v)
                if msg:
                    problems.setdefault(msg, [[], []])
                    problems[msg][0].append(r)
                    if v not in problems[msg][1]:
                        problems[msg][1].append(v)
            # ranges / mandatory '?' still need checking on the fast path
            rs = self._parsed_ranges(cat, item)
            if rs or kw.mandatory:
                self._dense_ranges(table, item, rs, kw, problems)
            return {m: (np.asarray(sorted(set(r)), dtype=np.int64), v)
                    for m, (r, v) in problems.items()}
        uniques, codes = col.factorize()
        bad: Dict[str, List[int]] = {}
        for code, v in enumerate(uniques):
            msg = self.check_value(cat, item, v)
            if msg:
                bad.setdefault(msg, []).append(code)
        out = {}
        for msg, bcodes in bad.items():
            rows = np.flatnonzero(np.isin(codes, np.asarray(bcodes)))
            out[msg] = (rows, [uniques[c] for c in bcodes[:MAX_EXAMPLES]])
        return out

    def _dense_ranges(self, table, item, rs, kw, problems):
        arr = table.columns[item].arr
        if kw.mandatory:
            rows = np.flatnonzero(arr == b"?")
            if len(rows):
                msg = "mandatory item has value '?'"
                problems.setdefault(msg, [[], []])
                problems[msg][0].extend(rows.tolist())
                problems[msg][1].append("?")
        if not rs:
            return
        nulls = (arr == b"?") | (arr == b".")
        idx = np.flatnonzero(~nulls)
        try:
            x = arr[idx].astype(np.float64)
        except ValueError:
            return
        ok = np.zeros(len(x), dtype=bool)
        for lo, hi in rs:
            if lo is not None and hi is not None and lo == hi:
                ok |= x == lo
            else:
                m = np.ones(len(x), dtype=bool)
                if lo is not None:
                    m &= x > lo
                if hi is not None:
                    m &= x < hi
                ok |= m
        bad = idx[~ok]
        if len(bad):
            msg = "out of allowed range (%s)" % self.range_text(table.name,
                                                                item)
            problems.setdefault(msg, [[], []])
            problems[msg][0].extend(bad.tolist())
            problems[msg][1].extend(sorted(set(
                table.columns[item].take(bad[:MAX_EXAMPLES]))))

    def check_table(self, table: Table) -> List[Issue]:
        issues: List[Issue] = []
        cat = table.name
        dcat = self.dic.ihm.categories.get(cat) if self.dic.ihm else None
        if dcat is None:
            issues.append(Issue(ERROR, "unknown_category", cat,
                                "category is not defined in the dictionary",
                                count=table.nrows))
            return issues
        for item in table.items:
            if item not in dcat.keywords:
                issues.append(Issue(ERROR, "unknown_item", cat,
                                    "item is not defined in the dictionary",
                                    items=[item], count=table.nrows))
        for kname, kw in dcat.keywords.items():
            if kw.mandatory and kname not in table.columns:
                issues.append(Issue(ERROR, "missing_item", cat,
                                    "mandatory item is missing",
                                    items=[kname]))
        for item in table.items:
            if item not in dcat.keywords:
                continue
            for msg, (rows, vals) in self._column_problems(table,
                                                           item).items():
                sev = WARNING if msg.startswith("out of allowed range") \
                    else ERROR
                issues.append(Issue(sev, "bad_value", cat, msg, items=[item],
                                    count=len(rows),
                                    rows=rows[:MAX_EXAMPLES].tolist(),
                                    values=list(vals)[:MAX_EXAMPLES]))
        issues.extend(self._duplicate_keys(table))
        return issues

    def _duplicate_keys(self, table: Table) -> List[Issue]:
        cm = self.dic.category(table.name)
        if cm is None or not cm.keys or table.nrows < 2:
            return []
        if not all(k in table.columns for k in cm.keys):
            return []
        if len(cm.keys) == 1 and isinstance(table.columns[cm.keys[0]],
                                            DenseColumn):
            return self._duplicate_dense(table, cm.keys[0])
        key = _combined_codes(table, cm.keys)
        if key is None:
            return []
        uniq, first, counts = np.unique(key, return_index=True,
                                        return_counts=True)
        dup = counts > 1
        if not dup.any():
            return []
        dup_keys = uniq[dup]
        rows = np.flatnonzero(np.isin(key, dup_keys))
        vals = []
        for r in first[dup][:MAX_EXAMPLES].tolist():
            vals.append(", ".join("%s=%s" % (k, table.get(r, k))
                                  for k in cm.keys))
        return [Issue(ERROR, "duplicate_key", table.name,
                      "%d key value(s) used by more than one row"
                      % int(dup.sum()), items=list(cm.keys),
                      count=len(rows), rows=rows[:MAX_EXAMPLES].tolist(),
                      values=vals)]

    def _duplicate_dense(self, table: Table, item: str) -> List[Issue]:
        arr = table.columns[item].arr
        uniq, counts = np.unique(arr, return_counts=True)
        dup = uniq[counts > 1]
        if not len(dup):
            return []
        rows = np.flatnonzero(np.isin(arr, dup))
        vals = ["%s=%s" % (item, table.get(int(r), item))
                for r in rows[:MAX_EXAMPLES]]
        return [Issue(ERROR, "duplicate_key", table.name,
                      "%d key value(s) used by more than one row" % len(dup),
                      items=[item], count=len(rows),
                      rows=rows[:MAX_EXAMPLES].tolist(), values=vals)]

    # -- links ----------------------------------------------------------------
    def parent_keyset(self, data: CifData, parent: Table,
                      items: Tuple[str, ...]) -> Set[Tuple[str, ...]]:
        key = (id(data), parent.name, items)
        ks = self._keysets.get(key)
        if ks is None:
            cols = [parent.columns[i].values(0, parent.nrows) for i in items]
            ks = set(zip(*cols))
            self._keysets[key] = ks
        return ks

    def check_links(self, data: CifData, table: Table) -> List[Issue]:
        issues = []
        for link in self.dic.parents_of(table.name):
            iss = self._check_link(data, table, link)
            if iss:
                issues.append(iss)
        return issues

    def _check_link(self, data: CifData, table: Table,
                    link: Link) -> Optional[Issue]:
        direct = link.child_items[:link.n_direct]
        if not all(c in table.columns for c in direct):
            return None
        pairs = [(c, p) for c, p in zip(link.child_items, link.parent_items)
                 if c in table.columns]
        citems = tuple(c for c, _ in pairs)
        pitems = tuple(p for _, p in pairs)
        facts = [table.columns[c].factorize() for c in citems]
        # rows where all direct items are non-null
        valid = np.ones(table.nrows, dtype=bool)
        for (uniq, codes), c in zip(facts, citems):
            if c in direct:
                nullc = [i for i, u in enumerate(uniq) if u in NULLS]
                if nullc:
                    valid &= ~np.isin(codes, nullc)
        idx = np.flatnonzero(valid)
        if len(idx) == 0:
            return None
        key = _mixed_radix([codes[idx] for _, codes in facts],
                           [len(u) for u, _ in facts])
        if key is None:
            return None
        ukeys, first, counts = np.unique(key, return_index=True,
                                         return_counts=True)
        parent = data.get(link.parent)
        if parent is None:
            if link.parent.startswith("chem_comp_"):
                return None
            sev = WARNING if link.parent.startswith("ihm_") else INFO
            return Issue(sev, "missing_parent_category", table.name,
                         "refers to category %s, which is not in the file"
                         % link.parent, items=list(direct),
                         count=int(counts.sum()),
                         rows=idx[first[:MAX_EXAMPLES]].tolist(),
                         link=link.id)
        if not all(p in parent.columns for p in pitems[:link.n_direct]):
            return None
        bad_count, bad_vals, bad_keys = 0, [], []
        keysets: Dict[Tuple[str, ...], Set] = {}
        for k, f, n in zip(ukeys.tolist(), first.tolist(), counts.tolist()):
            r = int(idx[f])
            vals = [table.columns[c].value(r) for c in citems]
            use = tuple(i for i, (c, v) in enumerate(zip(citems, vals))
                        if c in direct or v not in NULLS)
            use = tuple(i for i in use if pitems[i] in parent.columns)
            sub_p = tuple(pitems[i] for i in use)
            if sub_p not in keysets:
                keysets[sub_p] = self.parent_keyset(data, parent, sub_p)
            if tuple(vals[i] for i in use) not in keysets[sub_p]:
                bad_count += n
                bad_keys.append(k)
                if len(bad_vals) < MAX_EXAMPLES:
                    bad_vals.append(", ".join("%s=%s" % (citems[i], vals[i])
                                              for i in use))
        if not bad_count:
            return None
        bad_mask = np.isin(key, np.asarray(bad_keys))
        rows = idx[np.flatnonzero(bad_mask)[:MAX_EXAMPLES]]
        return Issue(ERROR, "broken_link", table.name,
                     "%d row(s) refer to %s rows that do not exist (%d "
                     "distinct value(s))" % (bad_count, link.parent,
                                             len(bad_keys)),
                     items=list(citems), count=bad_count,
                     rows=rows.tolist(), values=bad_vals, link=link.id)

    # -- everything -------------------------------------------------------
    def run(self, data: CifData,
            progress: Callable[[str, float], None] = lambda s, f: None
            ) -> List[Issue]:
        issues: List[Issue] = list(self.check_categories(data))
        if self.dic is None:
            return issues
        tables = list(data.tables.values())
        total = sum(max(t.nrows, 1) for t in tables) or 1
        done = 0
        for t in tables:
            progress("checking %s" % t.name, done / total)
            issues.extend(self.check_table(t))
            issues.extend(self.check_links(data, t))
            done += max(t.nrows, 1)
        progress("checked", 1.0)
        return issues

    def check_categories(self, data: CifData) -> List[Issue]:
        if self.dic is None or self.dic.ihm is None:
            return []
        out = []
        missing = sorted(c.name for c in self.dic.ihm.categories.values()
                         if c.mandatory and c.name not in data.tables)
        for name in missing:
            out.append(Issue(ERROR, "missing_category", name,
                             "mandatory category is missing from the file"))
        return out


def _combined_codes(table: Table, items: Sequence[str]
                    ) -> Optional[np.ndarray]:
    facts = [table.columns[i].factorize() for i in items]
    return _mixed_radix([c for _, c in facts], [len(u) for u, _ in facts])


def _mixed_radix(codes: List[np.ndarray], sizes: List[int]
                 ) -> Optional[np.ndarray]:
    """Combine several code arrays into one int64 key per row."""
    total = 1
    for s in sizes:
        total *= max(s, 1)
    if total >= 2 ** 62:
        return None
    key = np.zeros(len(codes[0]) if codes else 0, dtype=np.int64)
    for c, s in zip(codes, sizes):
        key = key * max(s, 1) + c.astype(np.int64)
    return key


def run_ihm_validator(path: str, fmt: str, gzipped: bool,
                      dic: DictInfo) -> List[str]:
    """Run python-ihm's dictionary validator; return its error messages."""
    if dic is None or dic.ihm is None:
        return ["no dictionary available"]
    opener = gzip.open if gzipped else open
    if fmt == "BCIF":
        with opener(path, "rb") as fh:
            return _validate(dic, fh, "BCIF")
    with opener(path, "rb") as raw:
        fh = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
        return _validate(dic, fh, "mmCIF")


def _validate(dic: DictInfo, fh, fmt: str) -> List[str]:
    try:
        dic.ihm.validate(fh, format=fmt)
    except ihm.dictionary.ValidatorError as exc:
        return [m for m in str(exc).split("\n\n") if m.strip()]
    return []
