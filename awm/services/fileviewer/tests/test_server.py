"""Unit tests for the fileviewer static-mount config: the mask and the status
snapshot.

fileviewer no longer runs its own HTTP listener — the file bytes ride the
gateway's ``kind=static`` mount, and mask *enforcement* is gateway-side (covered
by ``awm/gateway/tests/test_hub_static.py``). Here we test the pieces this
service owns: the default mask, the FILEVIEWER_MASK_FILE merge + self-hide, and
the status shape.
"""

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
def test_default_mask_unmasks_the_annex_object_store():
    # Every large file under a scope's annex-mode .awm/data is a symlink into
    # .git/annex/objects/, and the gateway matches the *resolved* path — so
    # without this negation the whole data tree 404s.
    m = server.DEFAULT_MASK
    assert "!**/.git/annex/objects/**" in m
    # Ordering is load-bearing: last match wins, so the negation must come
    # after **/.git/** (else .git stays fully masked) and before every
    # secret-shaped glob (else an annexed *.pem would be exposed).
    neg = m.index("!**/.git/annex/objects/**")
    assert m.index("**/.git/**") < neg
    for secret in ("**/*.pem", "**/*.key", "**/.env", "**/*.token",
                   "**/.ssh/**", "**/secrets/**"):
        assert m.index(secret) > neg, f"{secret!r} must follow the negation"


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
        st.prefix, st.root, st.deny = "/files", "/", server.DEFAULT_MASK
        st.mounted, st.service_id = True, "abc123"
    snap = st.snapshot()
    assert snap["mounted"] is True
    assert snap["prefix"] == "/files"
    assert snap["root"] == "/"
    assert snap["service_id"] == "abc123"
    assert snap["deny_globs"] == len(server.DEFAULT_MASK)
    assert snap["url_shape"].startswith("/files/")
