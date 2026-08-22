"""Tests for the attachment sink — and specifically for ``url``.

``path`` is absolute on the node that ran the download. When ``social`` is
borrowed from a peer that node is not the caller's, and every reply used to hand
back a path to a file the caller could never open. ``url`` is what makes the
bytes reachable, so the tests that matter are: it is present, it is right, and
it is absent (rather than wrong) when the file lies outside the mount root.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from awm.social import attachments


@pytest.fixture(autouse=True)
def _mount_at_root(monkeypatch, tmp_path):
    monkeypatch.delenv("FILEVIEWER_MOUNT_PREFIX", raising=False)
    monkeypatch.delenv("FILEVIEWER_MOUNT_ROOT", raising=False)
    # `tempfile` memoizes the default dir on first use, so setting $TMPDIR here
    # would be ignored — pin the module attribute the sink actually reads.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


def test_every_written_file_carries_a_url(tmp_path):
    out = attachments.write_attachments([("report.pdf", "application/pdf", b"%PDF")])

    (f,) = out
    assert f["size"] == 4
    assert os.path.isfile(f["path"])
    # Origin-relative, the mount prefix followed by the absolute path.
    assert f["url"] == "/files" + os.path.realpath(f["path"])


def test_url_is_percent_encoded(tmp_path, monkeypatch):
    """The sink's own sanitizer keeps names tame, but the temp root may not be."""
    root = tmp_path / "a b"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    (f,) = attachments.write_attachments([("x.txt", "text/plain", b"x")])
    assert " " not in f["url"]
    assert "%20" in f["url"]


def test_url_honours_a_narrowed_mount_root(tmp_path, monkeypatch):
    monkeypatch.setenv("FILEVIEWER_MOUNT_ROOT", str(tmp_path))
    (f,) = attachments.write_attachments([("x.txt", "text/plain", b"x")])
    # Relative to the root, not to /.
    assert f["url"].startswith("/files/")
    assert str(tmp_path) not in f["url"]


def test_url_is_none_outside_the_mount_root(tmp_path, monkeypatch):
    """An unreachable file says so; it does not advertise a URL that will 404."""
    monkeypatch.setenv("FILEVIEWER_MOUNT_ROOT", str(tmp_path / "elsewhere"))
    (tmp_path / "elsewhere").mkdir()
    (f,) = attachments.write_attachments([("x.txt", "text/plain", b"x")])
    assert f["url"] is None


def test_colliding_names_still_get_distinct_urls():
    out = attachments.write_attachments([
        ("draft.docx", "application/octet-stream", b"1"),
        ("draft.docx", "application/octet-stream", b"22"),
    ])
    assert out[0]["url"] != out[1]["url"]
    assert {os.path.getsize(f["path"]) for f in out} == {1, 2}
