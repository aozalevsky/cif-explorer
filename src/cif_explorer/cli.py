"""Command line entry point: ``cif_explorer FILE``."""
from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser

from . import __version__
from .dictionary import load_dictionary
from .explorer import Session
from .server import Watcher, make_server


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="cif_explorer",
        description="Explore an IHMCIF/mmCIF file in the browser: trace rows "
                    "up and down the category hierarchy, with dictionary "
                    "checks and live reload.")
    p.add_argument("file", help="mmCIF (.cif, .cif.gz) or BinaryCIF (.bcif)")
    p.add_argument("--port", type=int, default=0,
                   help="port to listen on (default: any free port)")
    p.add_argument("--host", default="127.0.0.1",
                   help="interface to bind (default: 127.0.0.1)")
    p.add_argument("--no-browser", action="store_true",
                   help="do not open a browser window")
    p.add_argument("--no-watch", action="store_true",
                   help="do not reload when the file changes")
    p.add_argument("--dict", action="append", metavar="DIC",
                   help="dictionary file to use (repeatable); default: PDBx "
                        "+ IHM dictionaries downloaded to the cache")
    p.add_argument("--update-dicts", action="store_true",
                   help="re-download the dictionaries")
    p.add_argument("--version", action="version",
                   version="%(prog)s " + __version__)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    path = os.path.abspath(args.file)
    if not os.path.isfile(path):
        sys.exit("cif_explorer: no such file: %s" % args.file)

    dic, messages = load_dictionary(args.dict, update=args.update_dicts)
    for m in messages:
        print("dictionary: %s" % m, file=sys.stderr)
    session = Session(path, dic, messages)

    httpd = make_server(session, host=args.host, port=args.port)
    host, port = httpd.server_address[:2]
    url = "http://%s:%d/" % ("localhost" if host in ("127.0.0.1", "::1")
                             else host, port)
    print("cif_explorer %s: %s" % (__version__, path))
    print("serving on %s  (Ctrl+C to stop)" % url, flush=True)

    threading.Thread(target=session.load, daemon=True).start()
    if not args.no_watch:
        Watcher(session).start()
    if not args.no_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
