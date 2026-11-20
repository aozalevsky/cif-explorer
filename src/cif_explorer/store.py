"""Compact in-memory storage of the categories of one mmCIF/BinaryCIF block.

All parsing is done by python-ihm (:class:`ihm.format.CifReader` or
:class:`ihm.format_bcif.BinaryCifReader`).  This module only stores the values
the reader hands over.  Small tables keep plain Python lists; tables with
many rows (atom_site, ihm_starting_model_coord, ihm_sphere_obj_site, ...) are
packed into numpy arrays batch by batch while reading, so memory stays a
fraction of the file size.
"""
from __future__ import annotations

import gzip
import io
import os
import time
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

import ihm.format
import ihm.format_bcif

NULLS = ("?", ".")
BATCH = 65536            # rows buffered before packing into numpy arrays
DENSE_FRACTION = 0.5     # >50% distinct values in a batch -> store densely
MAX_CATEGORIES = 1 << 20  # categorical columns switch to dense above this

ProgressFn = Callable[[str, float], None]


def _noop_progress(stage: str, fraction: float) -> None:
    pass


# --------------------------------------------------------------------------
# Columns
# --------------------------------------------------------------------------

class Column:
    """A column of string values."""

    def __len__(self) -> int:
        raise NotImplementedError

    def value(self, i: int) -> str:
        raise NotImplementedError

    def values(self, start: int, stop: int) -> List[str]:
        return [self.value(i) for i in range(start, stop)]

    def take(self, idx: Iterable[int]) -> List[str]:
        return [self.value(int(i)) for i in idx]

    def rows_eq(self, value: str) -> np.ndarray:
        """Indices of rows whose value equals ``value``."""
        raise NotImplementedError

    def factorize(self) -> Tuple[List[str], np.ndarray]:
        """Return (distinct values, per-row code into that list)."""
        raise NotImplementedError

    @property
    def nbytes(self) -> int:
        return 0


class ListColumn(Column):
    """Values held in a Python list (small tables)."""

    def __init__(self, values: Sequence[str]):
        self._values = list(values)
        self._index: Optional[Dict[str, np.ndarray]] = None
        self._fact = None

    def __len__(self):
        return len(self._values)

    def value(self, i):
        return self._values[i]

    def values(self, start, stop):
        return self._values[start:stop]

    def take(self, idx):
        v = self._values
        return [v[int(i)] for i in idx]

    def _build_index(self):
        index: Dict[str, list] = {}
        for i, v in enumerate(self._values):
            index.setdefault(v, []).append(i)
        self._index = {k: np.asarray(r, dtype=np.int64)
                       for k, r in index.items()}

    def rows_eq(self, value):
        if self._index is None:
            self._build_index()
        return self._index.get(value, np.empty(0, dtype=np.int64))

    def factorize(self):
        if self._fact is None:
            lookup: Dict[str, int] = {}
            for v in self._values:
                if v not in lookup:
                    lookup[v] = len(lookup)
            codes = np.fromiter(map(lookup.__getitem__, self._values),
                                dtype=np.int64, count=len(self._values))
            self._fact = (list(lookup), codes)
        return self._fact

    @property
    def nbytes(self):
        return sum(len(v) + 50 for v in self._values)


class CatColumn(Column):
    """Categorical column: distinct values plus a small integer code per row."""

    def __init__(self, uniques: List[str], codes: np.ndarray):
        self.uniques = uniques
        self.lookup = {v: i for i, v in enumerate(uniques)}
        self.codes = codes

    def __len__(self):
        return len(self.codes)

    def value(self, i):
        return self.uniques[self.codes[i]]

    def values(self, start, stop):
        u = self.uniques
        return [u[c] for c in self.codes[start:stop].tolist()]

    def take(self, idx):
        u = self.uniques
        return [u[c] for c in self.codes[np.asarray(idx, dtype=np.int64)]
                .tolist()]

    def rows_eq(self, value):
        code = self.lookup.get(value)
        if code is None:
            return np.empty(0, dtype=np.int64)
        return np.flatnonzero(self.codes == code)

    def factorize(self):
        return self.uniques, self.codes

    @property
    def nbytes(self):
        return self.codes.nbytes + sum(len(v) + 50 for v in self.uniques)


class DenseColumn(Column):
    """High-cardinality column (coordinates, ids) as a numpy bytes array."""

    def __init__(self, arr: np.ndarray):
        self.arr = arr
        self._is_bytes = arr.dtype.kind == "S"
        self._has_alpha: Optional[bool] = None

    @property
    def has_alpha(self) -> bool:
        """True if any value contains a letter (False for coordinates)."""
        if self._has_alpha is None:
            if self._is_bytes:
                self._has_alpha = bool(_ALPHA_LUT[self.arr.view(np.uint8)]
                                       .any())
            else:
                self._has_alpha = True
        return self._has_alpha

    def __len__(self):
        return len(self.arr)

    def _dec(self, v):
        return v.decode() if self._is_bytes else v

    def value(self, i):
        return self._dec(self.arr[i])

    def values(self, start, stop):
        return [self._dec(v) for v in self.arr[start:stop].tolist()]

    def take(self, idx):
        return [self._dec(v) for v in
                self.arr[np.asarray(idx, dtype=np.int64)].tolist()]

    def rows_eq(self, value):
        if self._is_bytes:
            try:
                key = value.encode("ascii")
            except UnicodeEncodeError:
                return np.empty(0, dtype=np.int64)
            return np.flatnonzero(self.arr == key)
        return np.flatnonzero(self.arr == value)

    def factorize(self):
        # not cached: for coordinates/ids this is millions of strings
        uniq, codes = np.unique(self.arr, return_inverse=True)
        return [self._dec(v) for v in uniq.tolist()], codes.astype(np.int64)

    @property
    def nbytes(self):
        return int(self.arr.nbytes)


_ALPHA_LUT = np.zeros(256, dtype=bool)
for _c in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz":
    _ALPHA_LUT[_c] = True


def _smallest_uint(n: int):
    if n <= 1 << 8:
        return np.uint8
    if n <= 1 << 16:
        return np.uint16
    return np.uint32


def _dense_array(vals: Sequence[str]) -> np.ndarray:
    try:
        return np.array(vals, dtype="S")
    except UnicodeEncodeError:
        return np.array(vals, dtype=object)


class _ColumnBuilder:
    """Accumulates batches of values for one column of a big table."""

    def __init__(self):
        self.mode: Optional[str] = None
        self.lookup: Dict[str, int] = {}
        self.uniques: List[str] = []
        self.parts: List[np.ndarray] = []

    def add(self, vals: Sequence[str]) -> None:
        if self.mode is None:
            nuniq = len(set(vals))
            self.mode = ("dense" if nuniq > 1024
                         and nuniq > DENSE_FRACTION * len(vals) else "cat")
        if self.mode == "cat":
            lookup = self.lookup
            for v in dict.fromkeys(vals):
                if v not in lookup:
                    lookup[v] = len(self.uniques)
                    self.uniques.append(v)
            dt = _smallest_uint(len(self.uniques))
            self.parts.append(np.fromiter(map(lookup.__getitem__, vals),
                                          dtype=dt, count=len(vals)))
            if len(self.uniques) > MAX_CATEGORIES:
                self._to_dense()
        else:
            self.parts.append(_dense_array(vals))

    def _to_dense(self):
        u = _dense_array(self.uniques)
        self.parts = [u[p] for p in self.parts]
        self.mode = "dense"
        self.lookup, self.uniques = {}, []

    def finish(self) -> Column:
        if self.mode == "cat":
            codes = (np.concatenate(self.parts) if self.parts
                     else np.empty(0, np.uint8))
            codes = codes.astype(_smallest_uint(len(self.uniques)),
                                 copy=False)
            return CatColumn(self.uniques, codes)
        if any(p.dtype == object for p in self.parts):
            self.parts = [p.astype("U").astype(object) if p.dtype.kind == "S"
                          else p for p in self.parts]
        return DenseColumn(np.concatenate(self.parts))


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------

class Table:
    """One mmCIF category."""

    def __init__(self, name: str, items: List[str], columns: List[Column],
                 line: Optional[int] = None,
                 item_lines: Optional[Dict[str, int]] = None):
        self.name = name.lower()            # no leading '_'
        #: keywords as written in the file (display), file order
        self.display = list(items)
        #: lowercase keywords (used for every lookup), file order
        self.items = [i.lower() for i in items]
        self.columns = dict(zip(self.items, columns))
        self.nrows = len(columns[0]) if columns else 0
        self.line = line                    # line of first keyword (mmCIF)
        self.item_lines = {k.lower(): v for k, v in (item_lines or {}).items()}
        self.packed = any(not isinstance(c, ListColumn) for c in columns)

    def __repr__(self):
        return "<Table %s %d rows x %d items>" % (self.name, self.nrows,
                                                 len(self.items))

    def has(self, item: str) -> bool:
        return item.lower() in self.columns

    def row(self, i: int) -> List[str]:
        return [self.columns[it].value(i) for it in self.items]

    def rows(self, start: int, stop: int) -> List[List[str]]:
        cols = [self.columns[it].values(start, stop) for it in self.items]
        return [list(r) for r in zip(*cols)]

    def take(self, idx: Sequence[int]) -> List[List[str]]:
        cols = [self.columns[it].take(idx) for it in self.items]
        return [list(r) for r in zip(*cols)]

    def get(self, i: int, item: str) -> Optional[str]:
        col = self.columns.get(item.lower())
        return None if col is None else col.value(i)

    def find(self, conds: Sequence[Tuple[str, str]]) -> np.ndarray:
        """Row indices where every (item, value) condition holds."""
        result: Optional[np.ndarray] = None
        for item, value in conds:
            col = self.columns.get(item.lower())
            if col is None:
                return np.empty(0, dtype=np.int64)
            rows = col.rows_eq(value)
            result = rows if result is None else np.intersect1d(
                result, rows, assume_unique=True)
            if len(result) == 0:
                break
        if result is None:
            return np.arange(self.nrows, dtype=np.int64)
        return result

    @property
    def nbytes(self) -> int:
        return sum(c.nbytes for c in self.columns.values())


# --------------------------------------------------------------------------
# Reading with python-ihm
# --------------------------------------------------------------------------

class _CountingReader(io.RawIOBase):
    """File wrapper that counts bytes consumed (for progress)."""

    def __init__(self, fh, total: int, callback: Callable[[float], None]):
        self._fh = fh
        self._total = max(total, 1)
        self._cb = callback
        self.pos = 0
        self._last = 0.0

    def _tick(self, n):
        self.pos += n
        frac = self.pos / self._total
        if frac - self._last >= 0.01 or frac >= 1.0:
            self._last = frac
            self._cb(min(frac, 1.0))

    def read(self, n=-1):
        b = self._fh.read(n)
        self._tick(len(b))
        return b

    def readinto(self, b):
        n = self._fh.readinto(b)
        self._tick(n or 0)
        return n

    def readable(self):
        return True

    def close(self):
        pass


def detect_format(path: str) -> Tuple[str, bool]:
    """Return (format, gzipped) where format is 'mmCIF' or 'BCIF'."""
    with open(path, "rb") as fh:
        magic = fh.read(2)
    gz = magic == b"\x1f\x8b"
    name = path.lower()
    if name.endswith(".gz"):
        name = name[:-3]
    return ("BCIF" if name.endswith(".bcif") else "mmCIF"), gz


class _Opened:
    """Context manager that opens (possibly gzipped) input with progress."""

    def __init__(self, path, progress: Callable[[float], None],
                 text: bool = False):
        self.path = path
        self.progress = progress
        self.text = text
        self._raw = None
        self._gz = None
        self._wrap = None

    def __enter__(self):
        self._raw = open(self.path, "rb")
        try:
            size = os.fstat(self._raw.fileno()).st_size
            magic = self._raw.read(2)
            self._raw.seek(0)
            counted = _CountingReader(self._raw, size, self.progress)
            if magic == b"\x1f\x8b":
                stream = self._gz = gzip.GzipFile(fileobj=counted, mode="rb")
            else:
                stream = io.BufferedReader(counted, buffer_size=65536)
            if self.text:
                self._wrap = io.TextIOWrapper(stream, encoding="utf-8",
                                              errors="replace")
                return self._wrap
            return stream
        except Exception:
            self._raw.close()
            raise

    def __exit__(self, *exc):
        try:
            if self._wrap is not None:
                self._wrap.detach()
            if self._gz is not None:
                self._gz.close()
        finally:
            if self._raw is not None:
                self._raw.close()


class _KeywordFinder:
    """Handler that wants no keywords, so every keyword in the category is
    reported (with its line number) to the unknown-keyword callback."""

    not_in_file = omitted = None
    unknown = "?"
    _keys: List[str] = []
    _int_keys = _float_keys = _bool_keys = frozenset()

    def __init__(self, category: str):
        self.category = category

    def __call__(self, *args):
        pass

    def end_save_frame(self):
        pass


class _RowSink:
    """Handler storing every row; packs big tables into numpy columns."""

    not_in_file = "?"
    omitted = "."
    unknown = "?"
    _int_keys = _float_keys = _bool_keys = frozenset()

    def __init__(self, category: str, keys: List[str]):
        self.category = category
        self._keys = keys
        self.rows: List[tuple] = []
        self.builders: Optional[List[_ColumnBuilder]] = None

    def __call__(self, *args):
        self.rows.append(args)
        if len(self.rows) >= BATCH:
            self._flush()

    def end_save_frame(self):
        pass

    def _flush(self):
        if not self.rows:
            return
        if self.builders is None:
            self.builders = [_ColumnBuilder() for _ in self._keys]
        cols = list(zip(*self.rows))
        self.rows = []
        for b, vals in zip(self.builders, cols):
            b.add(vals)

    def columns(self) -> List[Column]:
        if self.builders is None:
            if not self.rows:
                return [ListColumn([]) for _ in self._keys]
            return [ListColumn(c) for c in zip(*self.rows)]
        self._flush()
        cols = [b.finish() for b in self.builders]
        self.builders = None
        return cols


class CifData:
    """All categories of the first data block of a file."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        st = os.stat(self.path)
        self.size = st.st_size
        self.mtime = st.st_mtime
        self.format, self.gzipped = detect_format(self.path)
        self.tables: Dict[str, Table] = {}
        self.warnings: List[str] = []
        self.load_seconds = 0.0

    def __getitem__(self, name: str) -> Table:
        return self.tables[name]

    def get(self, name: str) -> Optional[Table]:
        return self.tables.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self.tables

    @property
    def nbytes(self) -> int:
        return sum(t.nbytes for t in self.tables.values())


def _reader(fmt: str, fh, handlers, ucat=None, ukey=None):
    if fmt == "BCIF":
        return ihm.format_bcif.BinaryCifReader(
            fh, handlers, unknown_category_handler=ucat,
            unknown_keyword_handler=ukey)
    return ihm.format.CifReader(fh, handlers, unknown_category_handler=ucat,
                                unknown_keyword_handler=ukey)


def load(path: str, known_categories: Iterable[str] = (),
         progress: ProgressFn = _noop_progress,
         keyword_order: Optional[Callable[[str], Sequence[str]]] = None
         ) -> CifData:
    """Read the first data block of ``path`` with python-ihm.

    ``known_categories`` (names without leading underscore, e.g. from the
    dictionary) lets keyword discovery happen in a single pass.
    ``keyword_order(category)`` gives the dictionary order of keywords; it is
    used for BinaryCIF, where python-ihm reports no line numbers.
    """
    t0 = time.time()
    data = CifData(path)
    fmt = data.format

    # -- pass 1: discover categories and keywords (python-ihm reports every
    #    keyword it was not asked for, together with its line number)
    cat_lines: Dict[str, Optional[int]] = {}
    keywords: Dict[str, Dict[str, Optional[int]]] = {}
    unknown_cats: Dict[str, Optional[int]] = {}

    def ukey(cat, key, line):
        cat = cat.lower()
        line = line or None          # BinaryCIF reports 0
        keywords.setdefault(cat, {}).setdefault(key, line)
        if cat not in cat_lines:
            cat_lines[cat] = line

    def ucat(cat, line):
        unknown_cats.setdefault(cat.lower(), line or None)

    def discover(cats, stage):
        handlers = {c: _KeywordFinder(c) for c in cats}
        with _Opened(path, lambda f: progress(stage, f),
                     text=(fmt != "BCIF")) as fh:
            r = _reader(fmt, fh, handlers, ucat, ukey)
            more = r.read_file()
        return more

    known = ["_" + c.lower() for c in known_categories]
    more_blocks = discover(known, "scanning")
    if unknown_cats:
        todo = dict(unknown_cats)
        unknown_cats.clear()
        discover(list(todo), "scanning unknown categories")
        for c, line in todo.items():
            cat_lines.setdefault(c, line)
            keywords.setdefault(c, {})
    if more_blocks:
        data.warnings.append("File has more than one data block; only the "
                             "first one is shown.")

    # -- pass 2: read every value
    sinks = {}
    for c, kw in keywords.items():
        keys = list({k.lower(): k for k in reversed(list(kw))}.values())[::-1]
        if all(line is None for line in kw.values()):
            # no line numbers (BinaryCIF): dictionary order, then the rest
            order = [k.lower() for k in (keyword_order(c.lstrip("_"))
                                         if keyword_order else ())]
            pos = {k: i for i, k in enumerate(order)}
            keys.sort(key=lambda k: (pos.get(k.lower(), len(pos)),
                                     k.lower()))
        if keys:
            sinks[c] = _RowSink(c, keys)
    with _Opened(path, lambda f: progress("reading", f),
                 text=(fmt != "BCIF")) as fh:
        r = _reader(fmt, fh, sinks)
        r.read_file()

    order = sorted(sinks, key=lambda c: (cat_lines.get(c) is None,
                                         cat_lines.get(c) or 0))
    for c in order:
        sink = sinks[c]
        cols = sink.columns()
        name = c[1:] if c.startswith("_") else c
        data.tables[name] = Table(name, sink._keys, cols,
                                  line=cat_lines.get(c),
                                  item_lines=keywords.get(c))
    data.load_seconds = time.time() - t0
    progress("ready", 1.0)
    return data
