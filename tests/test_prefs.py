import json
import threading
import urllib.request

from cif_explorer import prefs
from cif_explorer.server import make_server, render_index


def test_defaults_and_update():
    assert prefs.load() == {"theme": "dark"}
    got = prefs.update({"theme": "light", "trace_width": 512.6,
                        "bogus": 1, "issues_height": -3, "sidebar_width": True})
    assert got == {"theme": "light", "trace_width": 512}
    assert prefs.load() == got
    assert json.loads(prefs.prefs_path().read_text()) == got
    # None goes back to the default; invalid themes are ignored
    got = prefs.update({"theme": None, "trace_width": None})
    assert got == {"theme": "dark"}
    assert prefs.update({"theme": "pink"}) == {"theme": "dark"}


def test_broken_file_is_ignored():
    prefs.prefs_path().parent.mkdir(parents=True)
    prefs.prefs_path().write_text("{not json")
    assert prefs.load() == {"theme": "dark"}
    assert prefs.update({"theme": "light"})["theme"] == "light"


def test_index_gets_saved_prefs():
    html = render_index({"theme": "light", "sidebar_width": 300}).decode()
    assert '<html lang="en" data-theme="light">' in html
    blob = html.split('<script id="prefs" type="application/json">')[1]
    assert json.loads(blob.split("</script>")[0]) == {
        "theme": "light", "sidebar_width": 300}
    # the default page is unchanged apart from the values
    assert '<html lang="en" data-theme="dark">' in render_index(
        {"theme": "dark"}).decode()


def test_prefs_http():
    httpd = make_server(None, port=0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port
    try:
        req = urllib.request.Request(
            base + "/api/prefs", method="POST",
            data=json.dumps({"theme": "light"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            assert json.loads(r.read())["theme"] == "light"
        with urllib.request.urlopen(base + "/api/prefs") as r:
            assert json.loads(r.read()) == {"theme": "light"}
        with urllib.request.urlopen(base + "/") as r:
            assert b'data-theme="light"' in r.read()
    finally:
        httpd.shutdown()
        httpd.server_close()
