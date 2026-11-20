"""Session state and the JSON views served to the browser.

A :class:`Session` owns the currently loaded :class:`~cif_explorer.store.CifData`
and swaps in a new one atomically when the file changes, so the browser keeps
its view. Rows are addressed by *refs* that survive reloads: the category key
values when the dictionary defines a key present in the file, otherwise the
row index.
"""
from __future__ import annotations

import json
import os
import re
import queue
import textwrap
import threading
import time
import traceback
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import store
from .checks import Checker, Issue, run_ihm_validator
from .dictionary import DictInfo, Link
from .store import NULLS, CatColumn, CifData, DenseColumn, Table

MAX_PAGE = 1000
LABEL_ITEMS = (
    "name", "model_name", "group_name", "ensemble_name", "pdbx_description",
    "entity_description", "description", "details", "title", "data_type",
    "type", "method", "method_name", "restraint_type", "protocol_name",
    "step_name", "step_method", "starting_model_source", "content_type",
    "db_name", "database_name", "feature_type", "mon_id", "symbol",
)
# label templates: {a|b} takes the first non-null of a, b; " · " separated
# parts whose fields are all null are dropped
SPECIAL_LABELS = {
    "atom_site": "{label_comp_id} {label_seq_id} {label_atom_id}"
                 " · asym {label_asym_id} · model {ihm_model_id|"
                 "pdbx_pdb_model_num}",
    "ihm_starting_model_coord": "{comp_id} {seq_id} {atom_id} · asym "
                                "{asym_id} · starting model "
                                "{starting_model_id}",
    "ihm_sphere_obj_site": "asym {asym_id} · {seq_id_begin}-{seq_id_end}"
                           " · model {model_id}",
    "ihm_gaussian_obj_site": "asym {asym_id} · {seq_id_begin}-{seq_id_end}"
                             " · model {model_id}",
    "entity_poly_seq": "entity {entity_id} · {mon_id} {num}",
    "pdbx_poly_seq_scheme": "asym {asym_id} · {mon_id} {seq_id}",
    "ihm_poly_atom_feature": "asym {asym_id} · {comp_id} {seq_id} {atom_id}",
    "ihm_poly_residue_feature": "asym {asym_id} · {comp_id_begin} "
                                "{seq_id_begin}-{comp_id_end} {seq_id_end}",
}
_FIELD_RE = re.compile(r"\{([^}]+)\}")


def _template_label(t: Table, i: int, template: str) -> str:
    def first_value(alternatives: str) -> Optional[str]:
        for item in alternatives.split("|"):
            if t.has(item):
                v = t.get(i, item)
                if v not in NULLS:
                    return v
        return None

    parts = []
    for part in template.split(" · "):
        values = {m.group(1): first_value(m.group(1))
                  for m in _FIELD_RE.finditer(part)}
        if any(v is not None for v in values.values()):
            text = part
            for key, v in values.items():
                text = text.replace("{%s}" % key, v or "?")
            parts.append(text)
    return " · ".join(parts)


def _clean_text(s: Optional[str]) -> str:
    """Dictionary text: undo the indentation and hard wrapping of prose,
    keep indented (preformatted) blocks."""
    if not s:
        return ""
    lines = s.strip("\n").splitlines()
    first = lines[0].strip()
    rest = textwrap.dedent("\n".join(lines[1:]))
    text = first + ("\n" + rest if rest.strip() else "")
    out, cur = [], []
    for line in text.splitlines():
        if not line.strip():
            if cur:
                out.append(" ".join(cur))
                cur = []
            out.append("")
        elif line[:1] in (" ", "\t"):
            if cur:
                out.append(" ".join(cur))
                cur = []
            out.append(line.rstrip())
        else:
            cur.append(line.strip())
    if cur:
        out.append(" ".join(cur))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------

class Session:
    def __init__(self, path: str, dic: Optional[DictInfo],
                 dict_messages: Sequence[str] = ()):
        self.path = os.path.abspath(path)
        self.dic = dic
        self.dict_messages = list(dict_messages)
        self.data: Optional[CifData] = None
        self.checker = Checker(dic)
        self.version = 0
        self.state = "loading"            # loading | ready | error
        self.loading = False              # a (re)load is in progress
        self.error: Optional[str] = None  # last load error (file kept)
        self.progress = {"stage": "starting", "fraction": 0.0}
        self.issues: Optional[List[Issue]] = None
        self.issues_version = -1
        self.check_progress = {"stage": "", "fraction": 0.0}
        self.validator = {"state": "idle", "messages": [], "version": None,
                          "seconds": None}
        self._lock = threading.RLock()
        self._load_lock = threading.Lock()
        self._listeners: List[queue.Queue] = []
        self._last_progress_sent = 0.0
        self.loaded_stat: Optional[Tuple[float, int]] = None

    # -- events -------------------------------------------------------------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._listeners.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._listeners:
                self._listeners.remove(q)

    def broadcast(self, event: dict) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for q in listeners:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def _progress(self, stage: str, fraction: float, kind="load"):
        target = self.progress if kind == "load" else self.check_progress
        target["stage"], target["fraction"] = stage, fraction
        now = time.time()
        if now - self._last_progress_sent > 0.25 or fraction >= 1.0:
            self._last_progress_sent = now
            self.broadcast({"type": "status", "status": self.status()})

    # -- loading ------------------------------------------------------------
    def load(self) -> bool:
        """(Re)load the file. On failure the previous data stays active."""
        with self._load_lock:
            self.loading = True
            try:
                st = os.stat(self.path)
                stat_key = (st.st_mtime, st.st_size)
                # remember what we tried, so a broken file is not re-read
                # until it changes again
                self.loaded_stat = stat_key
                cats = self.dic.categories.keys() if self.dic else ()
                data = store.load(self.path, cats, progress=self._progress)
            except Exception as exc:  # parse errors, I/O errors
                if self.loaded_stat is None:
                    self.loaded_stat = (0.0, -1)
                self.error = "%s: %s" % (type(exc).__name__, exc)
                if self.data is None:
                    self.state = "error"
                self.loading = False
                self.progress = {"stage": "failed", "fraction": 0.0}
                self.broadcast({"type": "error", "message": self.error,
                                "status": self.status()})
                return False
            with self._lock:
                self.data = data
                self.version += 1
                self.state = "ready"
                self.loading = False
                self.error = None
                self.checker = Checker(self.dic)
                self.issues = None
            self.broadcast({"type": "reload", "version": self.version,
                            "status": self.status()})
            self.run_checks()
            return True

    def run_checks(self) -> None:
        data, version, checker = self.data, self.version, self.checker
        if data is None:
            return
        try:
            issues = checker.run(data, progress=lambda s, f: self._progress(
                s, f, kind="check"))
        except Exception:
            traceback.print_exc()
            issues = [Issue("error", "internal", "", "checks failed: see "
                            "terminal for details")]
        with self._lock:
            if version != self.version:
                return
            self.issues = issues
            self.issues_version = version
        self.broadcast({"type": "issues", "version": version,
                        "status": self.status()})

    def start_validator(self) -> bool:
        with self._lock:
            if self.validator["state"] == "running" or self.data is None:
                return False
            self.validator = {"state": "running", "messages": [],
                              "version": self.version, "seconds": None}
            data, data_version = self.data, self.version
        self.broadcast({"type": "status", "status": self.status()})

        def work():
            t0 = time.time()
            try:
                msgs = run_ihm_validator(data.path, data.format, data.gzipped,
                                         self.dic)
            except Exception as exc:
                msgs = ["validator failed: %s: %s" % (type(exc).__name__,
                                                      exc)]
            self.validator = {"state": "done", "messages": msgs[:2000],
                              "truncated": len(msgs) > 2000,
                              "version": data_version,
                              "seconds": round(time.time() - t0, 1)}
            self.broadcast({"type": "validator", "status": self.status()})
        threading.Thread(target=work, daemon=True).start()
        return True

    def file_changed(self) -> bool:
        try:
            st = os.stat(self.path)
        except OSError:
            return False
        return (st.st_mtime, st.st_size) != self.loaded_stat

    # -- status -------------------------------------------------------------
    def status(self) -> dict:
        data = self.data
        counts = {"error": 0, "warning": 0, "info": 0}
        if self.issues is not None:
            for i in self.issues:
                counts[i.severity] = counts.get(i.severity, 0) + 1
        return {
            "state": self.state,
            "loading": self.loading,
            "version": self.version,
            "error": self.error,
            "progress": self.progress,
            "checks": ("done" if self.issues is not None
                       else ("running" if data is not None else "waiting")),
            "check_progress": self.check_progress,
            "issue_counts": counts,
            "validator": {k: v for k, v in self.validator.items()
                          if k != "messages"},
            "file": {
                "path": self.path, "name": os.path.basename(self.path),
                "size": data.size if data else None,
                "format": data.format if data else None,
                "gzipped": data.gzipped if data else None,
                "load_seconds": round(data.load_seconds, 2) if data else None,
                "memory": data.nbytes if data else None,
                "mtime": data.mtime if data else None,
            },
            "dictionary": (self.dic.version_string if self.dic else None),
            "dictionary_messages": self.dict_messages,
        }


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------

class NotFound(Exception):
    pass


class Views:
    """Builds JSON-able responses from one consistent (data, dic) pair."""

    def __init__(self, session: Session):
        self.session = session
        self.data: CifData = session.data
        self.dic: Optional[DictInfo] = session.dic
        self.checker: Checker = session.checker
        if self.data is None:
            raise NotFound("no data loaded")

    # -- helpers -----------------------------------------------------------
    def table(self, cat: str) -> Table:
        t = self.data.get((cat or "").lower())
        if t is None:
            raise NotFound("category %s not in file" % cat)
        return t

    def key_items(self, t: Table) -> List[str]:
        cm = self.dic.category(t.name) if self.dic else None
        if cm and cm.keys and all(k in t.columns for k in cm.keys):
            return list(cm.keys)
        return []

    def ref(self, t: Table, i: int) -> str:
        keys = self.key_items(t)
        if keys:
            return "k:" + json.dumps([t.get(i, k) for k in keys])
        return "i:%d" % i

    def resolve(self, t: Table, ref: str) -> Optional[int]:
        if ref.startswith("i:"):
            i = int(ref[2:])
            return i if 0 <= i < t.nrows else None
        if ref.startswith("k:"):
            keys = self.key_items(t)
            vals = json.loads(ref[2:])
            if not keys or len(keys) != len(vals):
                return None
            rows = t.find(list(zip(keys, vals)))
            return int(rows[0]) if len(rows) else None
        return None

    def label(self, t: Table, i: int) -> str:
        template = SPECIAL_LABELS.get(t.name)
        if template:
            label = _template_label(t, i, template)
            if label:
                return label
        keys = self.key_items(t)
        parts = ["%s=%s" % (k, t.get(i, k)) for k in keys]
        for item in LABEL_ITEMS:
            if item in keys or not t.has(item):
                continue
            v = t.get(i, item)
            if v and v not in NULLS:
                v = " ".join(v.split())
                parts.append(v if len(v) <= 70 else v[:67] + "...")
                if len(parts) >= len(keys) + 2:
                    break
        return " · ".join(parts) if parts else "row %d" % (i + 1)

    def row_summary(self, t: Table, i: int) -> dict:
        return {"cat": t.name, "idx": int(i), "ref": self.ref(t, i),
                "label": self.label(t, i)}

    def display(self, t: Table, item: str) -> str:
        idx = t.items.index(item) if item in t.items else -1
        if idx >= 0:
            return t.display[idx]
        return self.dic.display_name(t.name, item) if self.dic else item

    # -- link resolution --------------------------------------------------
    def _parent_conds(self, t: Table, i: int, link: Link
                      ) -> Tuple[Optional[list], list]:
        """Conditions on the parent for row i of child table t, and the
        (child item, value) pairs used. None if not applicable."""
        conds, used = [], []
        for n, (c, p) in enumerate(zip(link.child_items, link.parent_items)):
            direct = n < link.n_direct
            if not t.has(c):
                if direct:
                    return None, []
                continue
            v = t.get(i, c)
            if v in NULLS:
                if direct:
                    return None, [(c, v)]
                continue
            conds.append((p, v))
            used.append((c, v))
        return conds, used

    def _child_conds(self, parent: Table, i: int, link: Link,
                     child: Table) -> Optional[list]:
        conds = []
        for n, (c, p) in enumerate(zip(link.child_items, link.parent_items)):
            direct = n < link.n_direct
            if not child.has(c) or not parent.has(p):
                if direct:
                    return None
                continue
            v = parent.get(i, p)
            if v in NULLS:
                if direct:
                    return None
                continue
            conds.append((c, v))
        return conds

    def parents(self, t: Table, i: int) -> List[dict]:
        out = []
        if self.dic is None:
            return out
        for link in self.dic.parents_of(t.name):
            if not t.has(link.child_items[0]):
                continue
            conds, used = self._parent_conds(t, i, link)
            entry = {"link": link.id, "parent": link.parent,
                     "via": [{"child": c, "value": v} for c, v in used],
                     "items": list(link.child_items),
                     "parent_items": list(link.parent_items)}
            if conds is None:
                if not used:
                    continue
                entry["status"] = "null"
                out.append(entry)
                continue
            pt = self.data.get(link.parent)
            if pt is None:
                entry["status"] = "no_category"
                out.append(entry)
                continue
            if not all(pt.has(p) for p, _ in conds):
                entry["status"] = "no_item"
                out.append(entry)
                continue
            rows = pt.find(conds)
            entry["count"] = int(len(rows))
            entry["status"] = ("missing" if len(rows) == 0 else
                               "ok" if len(rows) == 1 else "ambiguous")
            entry["rows"] = [self.row_summary(pt, r) for r in rows[:20]]
            out.append(entry)
        return out

    def children_links(self, t: Table, i: int) -> List[dict]:
        out = []
        if self.dic is None:
            return out
        for link in self.dic.children_of(t.name):
            ct = self.data.get(link.child)
            if ct is None:
                continue
            conds = self._child_conds(t, i, link, ct)
            if conds is None:
                continue
            n = len(ct.find(conds))
            out.append({"link": link.id, "child": link.child,
                        "via": [{"child": c, "value": v} for c, v in conds],
                        "count": int(n)})
        out.sort(key=lambda e: (e["count"] == 0, e["child"]))
        return out

    def find_link(self, link_id: str) -> Link:
        for link in self.dic.links if self.dic else ():
            if link.id == link_id:
                return link
        raise NotFound("unknown link %s" % link_id)

    # -- public views -----------------------------------------------------
    def summary(self) -> dict:
        counts: Dict[str, Dict[str, int]] = {}
        issues = self.session.issues if (
            self.session.issues_version == self.session.version) else None
        for iss in issues or ():
            c = counts.setdefault(iss.category, {})
            c[iss.severity] = c.get(iss.severity, 0) + 1
        cats = []
        for t in self.data.tables.values():
            cm = self.dic.category(t.name) if self.dic else None
            known = bool(self.dic and self.dic.ihm and
                         t.name in self.dic.ihm.categories)
            cats.append({
                "name": t.name, "nrows": t.nrows, "ncols": len(t.items),
                "line": t.line, "packed": t.packed, "known": known,
                "groups": cm.groups if cm else [],
                "issues": counts.get(t.name, {}),
            })
        missing = [{"name": n, "issues": c} for n, c in counts.items()
                   if n not in self.data.tables]
        return {"categories": cats, "missing": missing,
                "warnings": self.data.warnings,
                "status": self.session.status()}

    def category_info(self, t: Table) -> dict:
        cm = self.dic.category(t.name) if self.dic else None
        parents = []
        children = []
        if self.dic:
            for link in self.dic.parents_of(t.name):
                if not t.has(link.child_items[0]):
                    continue
                parents.append({"cat": link.parent, "link": link.id,
                                "via": link.describe(),
                                "present": link.parent in self.data})
            for link in self.dic.children_of(t.name):
                if link.child in self.data:
                    children.append({"cat": link.child, "link": link.id,
                                     "via": link.describe(),
                                     "present": True})
        return {
            "name": t.name,
            "description": _clean_text(cm.description) if cm else "",
            "keys": cm.keys if cm else [],
            "groups": cm.groups if cm else [],
            "known": cm is not None,
            "parents": parents, "children": children,
            "line": t.line,
        }

    def item_info(self, cat: str, item: str) -> dict:
        im = self.dic.item(cat, item) if self.dic else None
        kw = self.dic.keyword(cat, item) if self.dic else None
        info = {"item": item, "known": kw is not None or im is not None}
        if im:
            info.update({
                "name": im.name, "description": _clean_text(im.description),
                "type": im.type_code, "units": im.units,
                "mandatory": bool(kw.mandatory) if kw else im.mandatory,
                "range": self.checker.range_text(cat, item) if im.ranges
                else None,
                "enum": [{"value": v, "detail": _clean_text(d)}
                         for v, d in im.enum[:200]],
                "enum_total": len(im.enum),
            })
            if kw and kw.item_type is not None:
                info["construct"] = kw.item_type.construct
        if self.dic:
            info["parents"] = sorted({l.parent for l in self.dic.parents_of(
                cat) if item.lower() in l.child_items})
            info["children"] = sorted({l.child for l in self.dic.children_of(
                cat) if item.lower() in l.parent_items})
        return info

    def table_page(self, cat: str, offset: int = 0, limit: int = 100,
                   filters: Sequence[Tuple[str, str]] = (), q: str = "",
                   around: Optional[int] = None) -> dict:
        t = self.table(cat)
        limit = max(1, min(int(limit), MAX_PAGE))
        filters = [(f.lower(), v) for f, v in filters]
        if filters or q:
            rows = t.find(filters) if filters else np.arange(
                t.nrows, dtype=np.int64)
            if q:
                rows = np.intersect1d(rows, search_table(t, q),
                                      assume_unique=True)
            total = len(rows)
        else:
            rows = None
            total = t.nrows
        if around is not None:
            pos = (int(np.searchsorted(rows, around)) if rows is not None
                   else around)
            offset = (pos // limit) * limit
        offset = max(0, min(int(offset), max(total - 1, 0)))
        if rows is None:
            idx = np.arange(offset, min(offset + limit, total))
            values = t.rows(offset, min(offset + limit, total))
        else:
            idx = rows[offset:offset + limit]
            values = t.take(idx)
        out_rows = []
        keyset_cache: Dict[str, object] = {}
        for i, vals in zip(idx.tolist(), values):
            flags = {}
            for j, (item, v) in enumerate(zip(t.items, vals)):
                msg = self.checker.check_value(t.name, item, v)
                if msg:
                    flags[j] = msg
            for j, msg in self._link_flags(t, i, keyset_cache):
                flags.setdefault(j, msg)
            out_rows.append({"idx": i, "ref": self.ref(t, i),
                             "values": vals, "flags": flags})
        return {
            "cat": t.name, "nrows": t.nrows, "total": total,
            "offset": offset, "limit": limit,
            "filters": [{"item": f, "value": v} for f, v in filters],
            "q": q,
            "items": [self._item_head(t, it, d) for it, d in
                      zip(t.items, t.display)],
            "rows": out_rows,
            "info": self.category_info(t),
        }

    def _item_head(self, t: Table, item: str, name: str) -> dict:
        kw = self.dic.keyword(t.name, item) if self.dic else None
        return {"item": item, "name": name,
                "known": kw is not None or self.dic is None,
                "mandatory": bool(kw.mandatory) if kw else False}

    def _link_flags(self, t: Table, i: int, cache) -> List[Tuple[int, str]]:
        """Cells of row i whose parent row does not exist."""
        out = []
        if self.dic is None:
            return out
        for link in self.dic.parents_of(t.name):
            pt = self.data.get(link.parent)
            if pt is None or not t.has(link.child_items[0]):
                continue
            conds, used = self._parent_conds(t, i, link)
            if not conds or not all(pt.has(p) for p, _ in conds):
                continue
            pitems = tuple(p for p, _ in conds)
            ks = self.checker.parent_keyset(self.data, pt, pitems)
            if tuple(v for _, v in conds) not in ks:
                msg = "no %s row with %s" % (
                    link.parent, ", ".join("%s=%s" % c for c in conds))
                for c, _ in used:
                    out.append((t.items.index(c), msg))
        return out

    def row_detail(self, cat: str, ref: str) -> dict:
        t = self.table(cat)
        i = self.resolve(t, ref)
        if i is None:
            raise NotFound("row %s no longer exists in %s" % (ref, cat))
        vals = t.row(i)
        link_flags = dict(self._link_flags(t, i, {}))
        fields = []
        for j, (item, v) in enumerate(zip(t.items, vals)):
            fields.append({"item": item, "name": t.display[j], "value": v,
                           "flag": (self.checker.check_value(t.name, item, v)
                                    or link_flags.get(j))})
        return {
            "cat": t.name, "idx": i, "ref": self.ref(t, i),
            "label": self.label(t, i), "nrows": t.nrows,
            "fields": fields,
            "parents": self.parents(t, i),
            "children": self.children_links(t, i),
        }

    def children(self, cat: str, ref: str, link_id: str, offset: int = 0,
                 limit: int = 50) -> dict:
        t = self.table(cat)
        i = self.resolve(t, ref)
        if i is None:
            raise NotFound("row %s no longer exists in %s" % (ref, cat))
        link = self.find_link(link_id)
        ct = self.table(link.child)
        conds = self._child_conds(t, i, link, ct) or []
        rows = ct.find(conds)
        limit = max(1, min(int(limit), MAX_PAGE))
        sel = rows[offset:offset + limit]
        return {"link": link_id, "child": ct.name, "count": int(len(rows)),
                "offset": offset,
                "filters": [{"item": c, "value": v} for c, v in conds],
                "rows": [self.row_summary(ct, r) for r in sel.tolist()]}

    def resolve_refs(self, refs: Sequence[dict]) -> List[Optional[dict]]:
        out = []
        for r in refs:
            try:
                t = self.table(r.get("cat"))
                i = self.resolve(t, r.get("ref", ""))
                out.append(None if i is None else self.row_summary(t, i))
            except (NotFound, ValueError, json.JSONDecodeError):
                out.append(None)
        return out

    def issues(self) -> dict:
        s = self.session
        ready = s.issues is not None and s.issues_version == s.version
        items = []
        if ready:
            order = {"error": 0, "warning": 1, "info": 2}
            for iss in sorted(s.issues, key=lambda x: (
                    order.get(x.severity, 3), x.category, x.items)):
                j = iss.to_json()
                t = self.data.get(iss.category)
                if t is not None:
                    j["refs"] = [self.ref(t, r) for r in iss.rows
                                 if r < t.nrows]
                items.append(j)
        return {"ready": ready, "issues": items,
                "validator": s.validator}

    def search(self, q: str, limit_per_cat: int = 5) -> dict:
        q = q.strip()
        results = []
        if not q:
            return {"q": q, "results": results}
        for t in self.data.tables.values():
            rows = search_table(t, q)
            if len(rows):
                results.append({
                    "cat": t.name, "count": int(len(rows)),
                    "rows": [self.row_summary(t, r)
                             for r in rows[:limit_per_cat].tolist()]})
        return {"q": q, "results": results}


def search_table(t: Table, q: str) -> np.ndarray:
    """Rows of t where any value contains q (case-insensitive)."""
    ql = q.lower()
    hits = []
    for col in t.columns.values():
        if isinstance(col, CatColumn):
            codes = [c for c, u in enumerate(col.uniques) if ql in u.lower()]
            if codes:
                hits.append(np.flatnonzero(np.isin(col.codes, codes)))
        elif isinstance(col, DenseColumn):
            arr = col.arr
            if arr.dtype.kind == "S":
                try:
                    qb = ql.encode("ascii")
                except UnicodeEncodeError:
                    continue
                if any(c.isalpha() for c in ql):
                    if not col.has_alpha:
                        continue      # e.g. coordinates
                    m = np.char.find(np.char.lower(arr), qb) >= 0
                else:
                    m = np.char.find(arr, qb) >= 0
            else:
                m = np.array([ql in str(v).lower() for v in arr])
            hits.append(np.flatnonzero(m))
        else:
            vals = col.values(0, len(col))
            hits.append(np.asarray([i for i, v in enumerate(vals)
                                    if ql in v.lower()], dtype=np.int64))
    if not hits:
        return np.empty(0, dtype=np.int64)
    return np.unique(np.concatenate(hits))
