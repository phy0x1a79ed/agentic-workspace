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

from awm.gateway.hub.registry import ServiceRecord
from awm.gateway.hub.static import serve_static


def _request(path: str, *, accept: str | None = None, query: str = "") -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if accept is not None:
        headers.append((b"accept", accept.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
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


def _rec(prefix: str, dir_: Path, *, entry: str | None = None,
         deny: tuple[str, ...] = ()) -> ServiceRecord:
    return ServiceRecord(
        name="t", prefix=prefix, kind="static",
        static_dir=str(dir_), entry=entry, deny=deny,
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


class TestDenyMask:
    """A per-mount ``deny`` glob list hides files: a matching path 404s exactly
    like a missing one, matched on the *resolved* (post-symlink) path so a
    symlink can't bypass the mask. Powers a broad mount (root ``/``) that must
    still hide secrets."""

    def test_masked_file_is_404(self, tmp_path):
        (tmp_path / "index.html").write_text("root")
        ssh = tmp_path / ".ssh"
        ssh.mkdir()
        (ssh / "id_ed25519").write_text("PRIVATE KEY")
        rec = _rec("/files", tmp_path, deny=("**/.ssh/**",))
        resp = _run(serve_static(_request("/files/.ssh/id_ed25519"), rec))
        assert resp.status_code == 404
        assert b"PRIVATE KEY" not in _read(resp)

    def test_unmasked_sibling_still_served(self, tmp_path):
        (tmp_path / "note.txt").write_text("visible\n")
        (tmp_path / "secret.pem").write_text("KEY")
        rec = _rec("/files", tmp_path, deny=("**/*.pem",))
        ok = _run(serve_static(_request("/files/note.txt"), rec))
        assert ok.status_code == 200
        assert _read(ok) == b"visible\n"
        masked = _run(serve_static(_request("/files/secret.pem"), rec))
        assert masked.status_code == 404

    def test_symlink_to_masked_file_is_404(self, tmp_path):
        # A symlink whose *name* is unmasked but which resolves to a masked
        # file must still 404 — matching is on the resolved path.
        (tmp_path / "index.html").write_text("root")
        secret = tmp_path / "real.pem"
        secret.write_text("KEY")
        link = tmp_path / "innocent.txt"
        try:
            link.symlink_to(secret)
        except OSError:
            pytest.skip("symlinks unsupported on this platform")
        rec = _rec("/files", tmp_path, deny=("**/*.pem",))
        resp = _run(serve_static(_request("/files/innocent.txt"), rec))
        assert resp.status_code == 404
        assert b"KEY" not in _read(resp)

    def test_no_deny_serves_everything(self, tmp_path):
        # Empty deny (the default for existing mounts) → zero behaviour change.
        (tmp_path / "secret.pem").write_text("KEY")
        rec = _rec("/files", tmp_path)  # deny=()
        resp = _run(serve_static(_request("/files/secret.pem"), rec))
        assert resp.status_code == 200
        assert _read(resp) == b"KEY"

    def test_masked_deep_path_is_404(self, tmp_path):
        deep = tmp_path / "home" / "u" / ".aws"
        deep.mkdir(parents=True)
        (deep / "credentials").write_text("aws_secret")
        rec = _rec("/files", tmp_path, deny=("**/.aws/**",))
        resp = _run(serve_static(
            _request("/files/home/u/.aws/credentials"), rec))
        assert resp.status_code == 404


class TestDenyNegation:
    """Globs are gitignore-shaped: a leading ``!`` re-exposes and the LAST
    matching glob wins. The motivating case is a git-annex working tree, where
    every large file is a symlink into ``.git/annex/objects/`` — masking
    ``**/.git/**`` on the resolved path would 404 the entire data tree."""

    def _annex_tree(self, tmp_path):
        """A miniature annex layout: a content-addressed object plus the
        working-tree symlink that points at it."""
        obj_dir = tmp_path / ".git" / "annex" / "objects" / "XG" / "KJ"
        obj_dir.mkdir(parents=True)
        obj = obj_dir / "SHA256E-s7--deadbeef.svg"
        obj.write_text("<svg/>")
        (tmp_path / "figures").mkdir()
        link = tmp_path / "figures" / "fig.svg"
        try:
            link.symlink_to(obj)
        except OSError:
            pytest.skip("symlinks unsupported on this platform")
        return obj

    def test_annex_symlink_served_through_negation(self, tmp_path):
        self._annex_tree(tmp_path)
        rec = _rec("/files", tmp_path,
                   deny=("**/.git/**", "!**/.git/annex/objects/**"))
        resp = _run(serve_static(_request("/files/figures/fig.svg"), rec))
        assert resp.status_code == 200
        assert _read(resp) == b"<svg/>"

    def test_annex_symlink_404s_without_negation(self, tmp_path):
        # The regression this negation exists to fix.
        self._annex_tree(tmp_path)
        rec = _rec("/files", tmp_path, deny=("**/.git/**",))
        resp = _run(serve_static(_request("/files/figures/fig.svg"), rec))
        assert resp.status_code == 404

    def test_negation_does_not_unmask_the_rest_of_dot_git(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("url = https://token@github")
        rec = _rec("/files", tmp_path,
                   deny=("**/.git/**", "!**/.git/annex/objects/**"))
        resp = _run(serve_static(_request("/files/.git/config"), rec))
        assert resp.status_code == 404

    def test_later_secret_glob_re_masks_after_negation(self, tmp_path):
        # Last-match-wins: a secret-shaped glob listed AFTER the negation still
        # hides an annexed object that carries that extension.
        obj_dir = tmp_path / ".git" / "annex" / "objects" / "aa" / "bb"
        obj_dir.mkdir(parents=True)
        (obj_dir / "SHA256E-s3--cafe.pem").write_text("KEY")
        rec = _rec("/files", tmp_path, deny=(
            "**/.git/**", "!**/.git/annex/objects/**", "**/*.pem",
        ))
        resp = _run(serve_static(_request(
            "/files/.git/annex/objects/aa/bb/SHA256E-s3--cafe.pem"), rec))
        assert resp.status_code == 404
        assert b"KEY" not in _read(resp)

    def test_bare_bang_is_ignored(self, tmp_path):
        # A lone "!" carries no pattern — it must not become a match-everything
        # unmask that voids the whole list.
        (tmp_path / "secret.pem").write_text("KEY")
        rec = _rec("/files", tmp_path, deny=("**/*.pem", "!"))
        resp = _run(serve_static(_request("/files/secret.pem"), rec))
        assert resp.status_code == 404


class TestTraversalContainment:
    def test_traversal_above_root_is_404(self, spa_bundle, tmp_path):
        (tmp_path.parent / "secret.txt").write_text("nope")
        rec = _rec("/app", spa_bundle)
        resp = _run(serve_static(_request("/app/../secret"), rec))
        assert resp.status_code == 404
        assert b"nope" not in _read(resp)


class TestPrefixRootTrailingSlashRedirect:
    """A bare prefix (``/app``) is the bundle's directory: redirect it to the
    canonical ``/app/`` so the bundle's relative ``./assets/...`` refs resolve
    against the bundle, not its parent. nginx/Apache/GitHub-Pages do the same.
    Only the prefix root redirects; sub-paths stay byte-serves.
    """

    def test_bare_prefix_redirects_to_trailing_slash(self, spa_bundle):
        rec = _rec("/app", spa_bundle)
        resp = _run(serve_static(_request("/app"), rec))
        assert resp.status_code == 301
        assert resp.headers["location"] == "/app/"

    def test_redirect_preserves_query_string(self, spa_bundle):
        rec = _rec("/app", spa_bundle)
        resp = _run(serve_static(_request("/app", query="x=1&y=2"), rec))
        assert resp.status_code == 301
        assert resp.headers["location"] == "/app/?x=1&y=2"

    def test_trailing_slash_serves_index_not_redirect(self, spa_bundle):
        rec = _rec("/app", spa_bundle)
        resp = _run(serve_static(_request("/app/"), rec))
        assert resp.status_code == 200
        assert b"root shell" in _read(resp)

    def test_naked_bundle_bare_prefix_redirects(self, naked_bundle):
        # No index.html but an ``entry`` → still served at the root, so the
        # bare prefix redirects to the slash form (then the auto-shell serves).
        rec = _rec("/c", naked_bundle, entry="main.js")
        resp = _run(serve_static(_request("/c"), rec))
        assert resp.status_code == 301
        assert resp.headers["location"] == "/c/"

    def test_bare_prefix_no_index_no_entry_is_404_not_redirect(self, naked_bundle):
        # Nothing to serve at the root → 404, never a redirect to a dead URL.
        rec = _rec("/c", naked_bundle)  # no entry, no index.html
        resp = _run(serve_static(_request("/c"), rec))
        assert resp.status_code == 404

    def test_subpath_directory_does_not_redirect(self, spa_bundle):
        # /app/focus is a sub-directory with its own index.html — the contract
        # serves it in place (no redirect); only the prefix ROOT redirects.
        rec = _rec("/app", spa_bundle)
        resp = _run(serve_static(_request("/app/focus"), rec))
        assert resp.status_code == 200
        assert b"focus shell" in _read(resp)
