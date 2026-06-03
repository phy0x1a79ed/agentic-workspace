"""Unit tests for ``serve_static`` — canonical-paths contract for kind=static mounts.

Every registered URL maps to one deterministic file on disk — either
that exact path or, if it points at a directory, that directory's
``index.html``. A miss is a 404, no matter the request headers or
extension. The only non-disk response is the auto-shell at the prefix
root for naked bundles (records carrying an ``entry``) — and only at
the root.
"""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.hub, pytest.mark.slow]

import asyncio
from pathlib import Path

import pytest
from starlette.requests import Request

from awm.services.hub.registry import ServiceRecord
from awm.services.hub.static import serve_static


def _request(path: str, *, accept: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if accept is not None:
        headers.append((b"accept", accept.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
    }
    return Request(scope)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _read(resp) -> bytes:
    if hasattr(resp, "path"):  # FileResponse
        return Path(resp.path).read_bytes()
    return resp.body


@pytest.fixture()
def spa_bundle(tmp_path: Path) -> Path:
    """A prerendered bundle: SvelteKit-style `<route>/index.html` shape."""
    (tmp_path / "index.html").write_text("<!doctype html><html>root shell</html>")
    (tmp_path / "favicon.svg").write_text("<svg/>")
    focus = tmp_path / "focus"
    focus.mkdir()
    (focus / "index.html").write_text("<!doctype html><html>focus shell</html>")
    immutable = tmp_path / "_app" / "immutable"
    immutable.mkdir(parents=True)
    (immutable / "entry.abc123.js").write_text("// chunk")
    return tmp_path


@pytest.fixture()
def naked_bundle(tmp_path: Path) -> Path:
    (tmp_path / "main.js").write_text("// main")
    (tmp_path / "style.css").write_text("body{}")
    return tmp_path


def _rec(prefix: str, dir_: Path, *, entry: str | None = None) -> ServiceRecord:
    return ServiceRecord(
        name="t", prefix=prefix, kind="static",
        static_dir=str(dir_), entry=entry,
    )


class TestCanonicalServing:
    def test_existing_file_served_verbatim(self, spa_bundle):
        rec = _rec("/app", spa_bundle)
        resp = _run(serve_static(_request("/app/favicon.svg"), rec))
        assert resp.status_code == 200
        assert _read(resp) == b"<svg/>"

    def test_root_returns_index_html(self, spa_bundle):
        rec = _rec("/app", spa_bundle)
        resp = _run(serve_static(_request("/app/"), rec))
        assert resp.status_code == 200
        assert b"root shell" in _read(resp)

    def test_directory_url_serves_index_html(self, spa_bundle):
        # /app/focus → focus/ directory has an index.html → served.
        # The canonical static-server default: directory URL serves the
        # directory's index file. Required for SvelteKit's
        # `<route>/index.html` prerender layout.
        rec = _rec("/app", spa_bundle)
        resp = _run(serve_static(_request("/app/focus"), rec))
        assert resp.status_code == 200
        assert b"focus shell" in _read(resp)

    def test_directory_url_with_trailing_slash_serves_index_html(self, spa_bundle):
        rec = _rec("/app", spa_bundle)
        resp = _run(serve_static(_request("/app/focus/"), rec))
        assert resp.status_code == 200
        assert b"focus shell" in _read(resp)


class TestCanonicalMissBehavior:
    def test_missing_path_is_404_no_accept_header(self, spa_bundle):
        rec = _rec("/app", spa_bundle)
        resp = _run(serve_static(_request("/app/no-such-route"), rec))
        assert resp.status_code == 404
        assert b"root shell" not in _read(resp)

    def test_missing_extensionless_path_is_404(self, spa_bundle):
        # The old SPA-fallback would have synthesized the shell here.
        # Canonical: no file (and no directory at this path), 404.
        rec = _rec("/app", spa_bundle)
        resp = _run(serve_static(_request("/app/focus/xii-hearth"), rec))
        assert resp.status_code == 404
        assert b"root shell" not in _read(resp)
        assert b"focus shell" not in _read(resp)

    def test_missing_path_with_html_accept_is_404(self, spa_bundle):
        # Browser refresh on an unrouted deep link with text/html in
        # Accept must return 404, not the root shell.
        rec = _rec("/app", spa_bundle)
        resp = _run(serve_static(
            _request("/app/nope", accept="text/html,application/xhtml+xml"), rec,
        ))
        assert resp.status_code == 404
        assert b"root shell" not in _read(resp)

    def test_missing_js_asset_is_404(self, spa_bundle):
        rec = _rec("/app", spa_bundle)
        resp = _run(serve_static(
            _request("/app/_app/immutable/missing.js", accept="*/*"), rec,
        ))
        assert resp.status_code == 404

    def test_missing_css_asset_is_404(self, spa_bundle):
        rec = _rec("/app", spa_bundle)
        resp = _run(serve_static(
            _request("/app/assets/missing.css", accept="text/css,*/*"), rec,
        ))
        assert resp.status_code == 404

    def test_directory_with_no_index_is_404(self, tmp_path):
        # A subdirectory that exists but has no index.html: there is no
        # canonical file to serve for the directory URL → 404.
        (tmp_path / "index.html").write_text("root")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "c.txt").write_text("gamma\n")
        rec = _rec("/canon", tmp_path)
        resp = _run(serve_static(_request("/canon/sub"), rec))
        assert resp.status_code == 404


class TestRegisterAndGet:
    """The user-story contract: register a directory, GET each file → exact bytes."""

    def test_register_then_get_returns_exact_bytes(self, tmp_path):
        (tmp_path / "a.txt").write_text("alpha\n")
        (tmp_path / "b.txt").write_text("beta\n")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "c.txt").write_text("gamma\n")

        rec = _rec("/canon", tmp_path)

        resp_a = _run(serve_static(_request("/canon/a.txt"), rec))
        assert resp_a.status_code == 200
        assert _read(resp_a) == b"alpha\n"

        resp_b = _run(serve_static(_request("/canon/b.txt"), rec))
        assert resp_b.status_code == 200
        assert _read(resp_b) == b"beta\n"

        resp_c = _run(serve_static(_request("/canon/sub/c.txt"), rec))
        assert resp_c.status_code == 200
        assert _read(resp_c) == b"gamma\n"

        resp_missing = _run(serve_static(_request("/canon/missing.txt"), rec))
        assert resp_missing.status_code == 404


class TestNakedBundleUnchanged:
    def test_root_returns_auto_shell(self, naked_bundle):
        rec = _rec("/c", naked_bundle, entry="main.js")
        resp = _run(serve_static(_request("/c/"), rec))
        assert resp.status_code == 200
        body = _read(resp).decode()
        assert "<script" in body and "main.js" in body

    def test_no_index_no_entry_root_is_404(self, naked_bundle):
        rec = _rec("/c", naked_bundle)  # no entry
        resp = _run(serve_static(_request("/c/"), rec))
        assert resp.status_code == 404

    def test_auto_shell_does_not_extend_to_subpaths(self, naked_bundle):
        # The shell is canonical at the prefix root only — never
        # synthesized for unrouted subpaths.
        rec = _rec("/c", naked_bundle, entry="main.js")
        resp = _run(serve_static(
            _request("/c/anywhere", accept="text/html"), rec,
        ))
        assert resp.status_code == 404


class TestTraversalContainment:
    def test_traversal_above_root_is_404(self, spa_bundle, tmp_path):
        (tmp_path.parent / "secret.txt").write_text("nope")
        rec = _rec("/app", spa_bundle)
        resp = _run(serve_static(_request("/app/../secret"), rec))
        assert resp.status_code == 404
        assert b"nope" not in _read(resp)
