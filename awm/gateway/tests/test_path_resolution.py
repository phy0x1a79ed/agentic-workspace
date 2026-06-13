"""Tests for awm.services._path.resolve_bin.

Systemd user units have a minimal PATH; resolve_bin extends it with
~/.local/bin and linuxbrew so bare subprocess calls don't die with
FileNotFoundError.
"""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.unit, pytest.mark.smoke]

import os
import stat

import pytest

from awm.gateway import _path


def _make_stub(dir_path, name):
    p = dir_path / name
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


def test_resolve_bin_finds_in_extra_path(tmp_path, monkeypatch):
    stub = _make_stub(tmp_path, "fakebin-xyz")
    monkeypatch.setattr(_path, "_EXTRA_PATHS", (str(tmp_path),))
    monkeypatch.setenv("PATH", "/nowhere")
    assert _path.resolve_bin("fakebin-xyz") == str(stub)


def test_resolve_bin_finds_in_inherited_path(tmp_path, monkeypatch):
    stub = _make_stub(tmp_path, "fakebin-pqr")
    monkeypatch.setattr(_path, "_EXTRA_PATHS", ())
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _path.resolve_bin("fakebin-pqr") == str(stub)


def test_resolve_bin_raises_with_search_path_in_message(tmp_path, monkeypatch):
    monkeypatch.setattr(_path, "_EXTRA_PATHS", (str(tmp_path),))
    monkeypatch.setenv("PATH", "/nowhere")
    with pytest.raises(RuntimeError) as ei:
        _path.resolve_bin("definitely-not-a-real-binary-abc123")
    msg = str(ei.value)
    assert "definitely-not-a-real-binary-abc123" in msg
    assert str(tmp_path) in msg
