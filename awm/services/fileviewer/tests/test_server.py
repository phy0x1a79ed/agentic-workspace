"""Unit tests for fileviewer's server: the mask, the status snapshot, and the
loopback listener the gateway mounts at /s/fileviewer."""

from __future__ import annotations

from pathlib import Path

import pytest

from awm.fileviewer import server


# ---- default mask ---------------------------------------------------------


@pytest.mark.smoke
def test_default_mask_covers_the_usual_secrets():
    m = server.DEFAULT_MASK
    for glob in ("**/.ssh/**", "**/*.pem", "**/*.key", "**/.env", "**/*.token",
                 "**/.aws/**", "**/.git/**"):
        assert glob in m, f"default mask missing {glob!r}"


@pytest.mark.smoke
def test_load_mask_defaults_only_when_no_override(monkeypatch):
    monkeypatch.delenv("FILEVIEWER_MASK_FILE", raising=False)
    assert server.load_mask() == tuple(dict.fromkeys(server.DEFAULT_MASK))


@pytest.mark.smoke
def test_load_mask_merges_override_and_self_hides(monkeypatch, tmp_path: Path):
    mask_file = tmp_path / "fileviewer.mask"
    mask_file.write_text(
        "# a comment\n"
        "**/private/**\n"
        "\n"
        "**/*.secretkey\n"
    )
    monkeypatch.setenv("FILEVIEWER_MASK_FILE", str(mask_file))
    # root == tmp_path so the mask file sits under the mount and self-hides.
    monkeypatch.setattr(server, "MOUNT_ROOT", str(tmp_path))

    globs = server.load_mask()
    # override globs merged in
    assert "**/private/**" in globs
    assert "**/*.secretkey" in globs
    # comments/blanks dropped
    assert "# a comment" not in globs
    assert "" not in globs
    # defaults still present
    assert "**/.ssh/**" in globs
    # the mask file hides itself, by basename and by path-relative-to-root
    assert f"**/{mask_file.name}" in globs
    assert mask_file.name in globs  # rel-to-root of tmp_path/fileviewer.mask


@pytest.mark.smoke
def test_load_mask_tolerates_unreadable_override(monkeypatch, tmp_path: Path):
    missing = tmp_path / "does-not-exist.mask"
    monkeypatch.setenv("FILEVIEWER_MASK_FILE", str(missing))
    monkeypatch.setattr(server, "MOUNT_ROOT", str(tmp_path))
    globs = server.load_mask()
    # defaults survive; the (missing) file still self-hides by basename
    assert "**/.ssh/**" in globs
    assert f"**/{missing.name}" in globs


# ---- status snapshot ------------------------------------------------------


@pytest.mark.smoke
def test_status_shape_before_mount():
    fresh = server._State()
    snap = fresh.snapshot()
    assert snap["mounted"] is False
    assert snap["prefix"] is None
    assert snap["url_shape"] is None
    assert snap["deny_globs"] == 0


@pytest.mark.smoke
def test_status_shape_when_mounted():
    st = server._State()
    with st.lock:
        st.prefix, st.root, st.deny = "/s/fileviewer", "/", server.DEFAULT_MASK
        st.mounted, st.url = True, "http://127.0.0.1:1234"
    snap = st.snapshot()
    assert snap["mounted"] is True
    assert snap["prefix"] == "/s/fileviewer"
    assert snap["root"] == "/"
    assert snap["listen"] == "http://127.0.0.1:1234"
    assert snap["deny_globs"] == len(server.DEFAULT_MASK)
    assert snap["url_shape"].startswith("/s/fileviewer/")


# ---- the listener ---------------------------------------------------------


@pytest.fixture
def served(tmp_path: Path):
    (tmp_path / "fig.svg").write_text("<svg/>")
    (tmp_path / "secret.pem").write_text("KEY")
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text("<p>hi</p>")
    (tmp_path / "bare").mkdir()
    (tmp_path / "link.txt").symlink_to(tmp_path / "secret.pem")
    srv = server.FileServer(str(tmp_path), ("**/*.pem",))
    import threading
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _get(srv, path: str, method: str = "GET"):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    conn.request(method, path)
    resp = conn.getresponse()
    return resp.status, resp.getheader("Content-Type"), resp.read()


@pytest.mark.smoke
def test_listener_serves_a_file_with_its_type(served):
    status, ctype, body = _get(served, "/fig.svg")
    assert (status, ctype, body) == (200, "image/svg+xml", b"<svg/>")


@pytest.mark.smoke
def test_listener_head_has_no_body(served):
    status, _, body = _get(served, "/fig.svg", "HEAD")
    assert (status, body) == (200, b"")


@pytest.mark.smoke
def test_listener_hides_masked_and_symlinked_secrets(served):
    assert _get(served, "/secret.pem")[0] == 404
    assert _get(served, "/link.txt")[0] == 404


@pytest.mark.smoke
def test_listener_refuses_traversal_and_missing(served):
    assert _get(served, "/../../etc/passwd")[0] == 404
    assert _get(served, "/%2e%2e/%2e%2e/etc/passwd")[0] == 404
    assert _get(served, "/nope.txt")[0] == 404


@pytest.mark.smoke
def test_listener_serves_directory_index_only(served):
    assert _get(served, "/site/")[2] == b"<p>hi</p>"
    assert _get(served, "/bare/")[0] == 404
