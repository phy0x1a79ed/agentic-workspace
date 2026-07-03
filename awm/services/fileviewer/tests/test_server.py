"""Unit tests for the fileviewer content-type logic and the HTTP handler.

Pure stdlib — spins the listener on an ephemeral loopback port and drives it
with urllib, asserting Content-Type + status per file kind and the 404 page.
"""

from __future__ import annotations

import socket
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from awm.fileviewer import server


# ---- content-type unit ----------------------------------------------------


@pytest.mark.smoke
def test_content_type_known_extensions(tmp_path: Path):
    assert server._content_type(Path("a.svg"), b"<svg/>") == "image/svg+xml"
    assert server._content_type(Path("a.html"), b"<h1>x</h1>") == "text/html; charset=utf-8"
    assert server._content_type(Path("a.png"), b"\x89PNG\r\n") == "image/png"
    # a known code extension keeps its text/* type + a charset (renders inline)
    py = server._content_type(Path("a.py"), b"print(1)\n")
    assert py.startswith("text/") and "charset=utf-8" in py
    # unknown extension, textual content -> text/plain so it displays inline
    assert server._content_type(Path("noext"), b"hello world") == "text/plain; charset=utf-8"
    # unknown extension, binary content -> octet-stream (download)
    assert server._content_type(Path("a.bin"), b"\x00\x01\x02") == "application/octet-stream"


@pytest.mark.smoke
def test_looks_textual():
    assert server._looks_textual(b"plain ascii")
    assert server._looks_textual("café".encode("utf-8"))
    assert not server._looks_textual(b"has a \x00 nul")
    assert not server._looks_textual(b"\xff\xfe\x00\x00")


# ---- end-to-end over a real socket ---------------------------------------


@pytest.fixture()
def live_server():
    # add_type registrations live in serve(); apply the couple the tests lean on.
    import mimetypes

    mimetypes.add_type("text/markdown", ".md")
    port = _free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), server.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get(url: str):
    try:
        resp = urllib.request.urlopen(url)
        return resp.status, resp.headers.get("Content-Type"), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), e.read()


@pytest.mark.smoke
def test_serves_svg(live_server, tmp_path: Path):
    f = tmp_path / "pic.svg"
    f.write_text('<svg xmlns="http://www.w3.org/2000/svg"><circle r="5"/></svg>')
    status, ctype, body = _get(f"{live_server}/?path={f}")
    assert status == 200
    assert ctype == "image/svg+xml"
    assert b"circle" in body


@pytest.mark.smoke
def test_serves_python_as_text(live_server, tmp_path: Path):
    f = tmp_path / "mod.py"
    f.write_text("print('hi')\n")
    status, ctype, body = _get(f"{live_server}/?path={f}")
    assert status == 200
    # renders inline as text (text/x-python on py3.14, text/plain elsewhere)
    assert ctype.startswith("text/") and "charset=utf-8" in ctype
    assert b"print" in body


@pytest.mark.smoke
def test_missing_path_404(live_server, tmp_path: Path):
    missing = tmp_path / "nope.txt"
    status, ctype, body = _get(f"{live_server}/?path={missing}")
    assert status == 404
    assert ctype.startswith("text/html")
    assert b"404" in body and b"nope.txt" in body


@pytest.mark.smoke
def test_directory_404(live_server, tmp_path: Path):
    status, ctype, body = _get(f"{live_server}/?path={tmp_path}")
    assert status == 404
    assert b"directory" in body.lower()


@pytest.mark.smoke
def test_no_path_404(live_server):
    status, ctype, body = _get(f"{live_server}/")
    assert status == 404
    assert b"No file path" in body
