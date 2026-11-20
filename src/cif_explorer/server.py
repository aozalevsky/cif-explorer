"""Local HTTP server: static UI, JSON API, server-sent events, file watcher."""
from __future__ import annotations

import json
import mimetypes
import os
import queue
import socket
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Optional
from urllib.parse import parse_qs, urlparse

from . import prefs
from .explorer import NotFound, Session, Views

STATIC = resources.files("cif_explorer") / "static"
HTML_TAG = '<html lang="en" data-theme="dark">'
PREFS_TAG = '<script id="prefs" type="application/json">{}</script>'


def render_index(saved: dict) -> bytes:
    """index.html with the saved preferences filled in, so the page starts
    in the right theme and panel sizes without a flash."""
    html = STATIC.joinpath("index.html").read_text(encoding="utf-8")
    theme = saved.get("theme", "dark")
    blob = json.dumps(saved).replace("<", "\\u003c")
    html = html.replace(HTML_TAG, HTML_TAG.replace("dark", theme, 1), 1)
    html = html.replace(PREFS_TAG, PREFS_TAG.replace("{}", blob, 1), 1)
    return html.encode("utf-8")


def _int(params, name, default):
    try:
        return int(params.get(name, [default])[0])
    except (TypeError, ValueError):
        return default


class Handler(BaseHTTPRequestHandler):
    server_version = "cif_explorer"
    session: Session = None  # set by make_server

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    # -- plumbing -------------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str,
              extra: Optional[dict] = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        body = json.dumps(obj, separators=(",", ":")).encode()
        self._send(code, body, "application/json")

    def _error(self, code, message):
        self._json({"error": message}, code)

    def _static(self, name: str):
        if not name or name.endswith("/"):
            name += "index.html"
        parts = name.split("/")
        if any(part in ("", ".", "..") or "\\" in part for part in parts):
            return self._error(404, "not found")
        res = STATIC.joinpath(*parts)
        if not res.is_file():
            return self._error(404, "not found")
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        body = (render_index(prefs.load()) if name == "index.html"
                else res.read_bytes())
        self._send(200, body, ctype)

    # -- routes ---------------------------------------------------------------
    def do_GET(self):
        url = urlparse(self.path)
        params = parse_qs(url.query, keep_blank_values=True)
        path = url.path
        try:
            if path == "/" or path.startswith("/static/"):
                return self._static(path[len("/static/"):]
                                    if path.startswith("/static/") else "")
            if path == "/api/events":
                return self._events()
            if path == "/api/status":
                return self._json(self.session.status())
            if path == "/api/prefs":
                return self._json(prefs.load())
            views = Views(self.session)
            p = lambda n, d="": params.get(n, [d])[0]  # noqa: E731
            if path == "/api/summary":
                return self._json(views.summary())
            if path == "/api/table":
                filters = []
                for f in params.get("f", []):
                    if ":" in f:
                        item, value = f.split(":", 1)
                        filters.append((item, value))
                return self._json(views.table_page(
                    p("cat"), offset=_int(params, "offset", 0),
                    limit=_int(params, "limit", 100), filters=filters,
                    q=p("q"), around=_int(params, "around", None)))
            if path == "/api/row":
                return self._json(views.row_detail(p("cat"), p("ref")))
            if path == "/api/children":
                return self._json(views.children(
                    p("cat"), p("ref"), p("link"),
                    offset=_int(params, "offset", 0),
                    limit=_int(params, "limit", 50)))
            if path == "/api/issues":
                return self._json(views.issues())
            if path == "/api/dict/item":
                return self._json(views.item_info(p("cat"), p("item")))
            if path == "/api/dict/category":
                return self._json(views.category_info(views.table(p("cat"))))
            if path == "/api/search":
                return self._json(views.search(p("q")))
            return self._error(404, "unknown endpoint")
        except NotFound as exc:
            return self._error(404, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            traceback.print_exc()
            return self._error(500, "%s: %s" % (type(exc).__name__, exc))

    def do_POST(self):
        url = urlparse(self.path)
        try:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._error(400, "invalid Content-Length")
            if n < 0:
                return self._error(400, "invalid Content-Length")
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._error(400, "invalid JSON body")
            if url.path == "/api/resolve":
                if not isinstance(body, dict) or not isinstance(
                        body.get("refs", []), list):
                    return self._error(400, "expected JSON object with 'refs'")
                views = Views(self.session)
                return self._json({"rows": views.resolve_refs(
                    body.get("refs", []))})
            if url.path == "/api/validate":
                started = self.session.start_validator()
                return self._json({"started": started})
            if url.path == "/api/prefs":
                return self._json(prefs.update(body))
            if url.path == "/api/reload":
                threading.Thread(target=self.session.load,
                                 kwargs={"background_checks": True},
                                 daemon=True).start()
                return self._json({"started": True})
            return self._error(404, "unknown endpoint")
        except NotFound as exc:
            return self._error(404, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            traceback.print_exc()
            return self._error(500, "%s: %s" % (type(exc).__name__, exc))

    def _events(self):
        q = self.session.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            first = {"type": "status", "status": self.session.status()}
            self.wfile.write(b"data: " + json.dumps(first).encode() + b"\n\n")
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=15)
                    msg = b"data: " + json.dumps(ev).encode() + b"\n\n"
                except queue.Empty:
                    msg = b": ping\n\n"
                self.wfile.write(msg)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.session.unsubscribe(q)


class Watcher(threading.Thread):
    """Polls the file; reloads once it has stopped changing."""

    def __init__(self, session: Session, interval: float = 1.0):
        super().__init__(daemon=True)
        self.session = session
        self.interval = interval
        self.stop_event = threading.Event()

    def _stat(self):
        try:
            st = os.stat(self.session.path)
            return st.st_mtime, st.st_size
        except OSError:
            return None

    def run(self):
        while not self.stop_event.wait(self.interval):
            loaded = self.session.loaded_stat
            if loaded is None:
                continue  # first load still running
            cur = self._stat()
            if cur is None or cur == loaded:
                continue
            # wait until the file is stable (editors write in steps)
            time.sleep(0.5)
            if self._stat() != cur:
                continue
            self.session.broadcast({"type": "changed"})
            self.session.load(background_checks=True)


def make_server(session: Session, host: str = "127.0.0.1",
                port: int = 0) -> ThreadingHTTPServer:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    server_cls = type("Server", (ThreadingHTTPServer,),
                      {"address_family": family})
    handler = type("BoundHandler", (Handler,), {"session": session})
    httpd = server_cls((host, port), handler)
    httpd.daemon_threads = True
    return httpd
