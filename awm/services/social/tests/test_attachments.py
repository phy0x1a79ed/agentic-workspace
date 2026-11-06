"""Tests for attachment replies: inline bytes for ``download_attachments``, and
``file_url`` for the files ``bucket_get`` writes to the sink.

``download_attachments`` never names a path, because ``social`` runs in a unit with
a private ``/tmp``; the bytes ride in the reply as ``b64``.
"""

from __future__ import annotations

import base64
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


def _sunk(name="x.txt"):
    path = os.path.join(attachments.sink_dir(), name)
    with open(path, "wb") as fh:
        fh.write(b"x")
    return path


def test_inline_entries_carry_the_bytes_and_no_path():
    (f,) = attachments.inline_attachments([("report.pdf", "application/pdf", b"%PDF")])
    assert base64.b64decode(f["b64"]) == b"%PDF"
    assert (f["filename"], f["mime"], f["size"]) == ("report.pdf", "application/pdf", 4)
    assert not {"path", "url"} & set(f)


def test_inline_binary_bytes_survive():
    data = bytes(range(256)) * 3
    (f,) = attachments.inline_attachments([("b.bin", "", data)])
    assert base64.b64decode(f["b64"]) == data
    assert f["mime"] == "application/octet-stream"


def test_inline_name_is_a_safe_basename_with_a_fallback():
    out = attachments.inline_attachments([
        ("../../etc/passwd", "", b"1"), ("", "", b"2"), ("a b.csv", "", b"3")])
    assert [f["filename"] for f in out] == ["passwd", "attachment-1", "a_b.csv"]


def test_inline_refuses_more_than_the_cap(monkeypatch):
    monkeypatch.setattr(attachments, "MAX_INLINE_BYTES", 5)
    attachments.inline_attachments([("a", "", b"12345")])
    with pytest.raises(ValueError, match="over the 5"):
        attachments.inline_attachments([("a", "", b"123"), ("b", "", b"456")])


def test_file_url_is_the_mount_prefix_plus_the_absolute_path():
    path = _sunk()
    assert attachments.file_url(path) == "/files" + os.path.realpath(path)


def test_file_url_is_percent_encoded(tmp_path, monkeypatch):
    root = tmp_path / "a b"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    url = attachments.file_url(_sunk())
    assert " " not in url and "%20" in url


def test_file_url_honours_a_narrowed_mount_root(tmp_path, monkeypatch):
    monkeypatch.setenv("FILEVIEWER_MOUNT_ROOT", str(tmp_path))
    url = attachments.file_url(_sunk())
    assert url.startswith("/files/") and str(tmp_path) not in url


def test_file_url_is_none_outside_the_mount_root(tmp_path, monkeypatch):
    """An unreachable file says so; it does not advertise a URL that will 404."""
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.setenv("FILEVIEWER_MOUNT_ROOT", str(tmp_path / "elsewhere"))
    assert attachments.file_url(_sunk()) is None
