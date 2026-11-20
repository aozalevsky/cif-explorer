"""UI preferences (theme, panel sizes) shared by every cif_explorer run.

They live in a small JSON file and not in the browser's localStorage,
because each run listens on a new port and localStorage is per port.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

THEMES = ("dark", "light")
DEFAULTS = {"theme": "dark"}
# panel sizes in CSS pixels; anything else is ignored
SIZES = ("sidebar_width", "trace_width", "issues_height")
MAX_SIZE = 10000

_lock = threading.Lock()


def config_dir() -> Path:
    env = os.environ.get("CIF_EXPLORER_CONFIG")
    if env:
        return Path(env)
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "cif_explorer"


def prefs_path() -> Path:
    return config_dir() / "prefs.json"


def _clean(raw) -> dict:
    """Keep only known keys with valid values."""
    out = {}
    if not isinstance(raw, dict):
        return out
    if raw.get("theme") in THEMES:
        out["theme"] = raw["theme"]
    for key in SIZES:
        v = raw.get(key)
        if (isinstance(v, (int, float)) and not isinstance(v, bool)
                and 0 < v <= MAX_SIZE):
            out[key] = int(v)
    return out


def load() -> dict:
    try:
        with open(prefs_path(), encoding="utf-8") as fh:
            saved = _clean(json.load(fh))
    except (OSError, ValueError):
        saved = {}
    return {**DEFAULTS, **saved}


def update(changes: dict) -> dict:
    """Merge `changes` into the saved preferences and return the result.

    A key whose value is None goes back to its default. Invalid keys and
    values are ignored.
    """
    if not isinstance(changes, dict):
        changes = {}
    with _lock:
        cur = load()
        cur.update(_clean(changes))
        for key, v in changes.items():
            if v is None and key in cur:
                del cur[key]
        cur = {**DEFAULTS, **cur}
        path = prefs_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".%d.part" % os.getpid())
            tmp.write_text(json.dumps(cur, indent=1, sort_keys=True) + "\n",
                           encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass  # read-only home: keep working with what we have
        return cur
