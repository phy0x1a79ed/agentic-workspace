"""The pin list the daemon reads: a JSON array under the CLI's lock directory."""

from __future__ import annotations

import json
import os
import time

import pytest

from awm.claudedaemon import pins


@pytest.fixture
def pins_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_CX_JOBS", str(tmp_path / "jobs"))
    path = tmp_path / "jobs" / "pins.json"
    path.parent.mkdir(parents=True)
    path.write_text("[]")
    return path


def test_pin_and_unpin_edit_the_array_under_the_cli_lock(pins_file):
    pins.pin("aaaa1111")
    pins.pin("bbbb2222")
    pins.pin("aaaa1111")
    assert json.loads(pins_file.read_text()) == ["aaaa1111", "bbbb2222"]
    assert pins.unpin("aaaa1111") == ["aaaa1111"]
    assert pins.pinned() == ["bbbb2222"]
    assert pins.unpin(["cccc3333"]) == []
    assert not (pins_file.parent / "pins.json.lock").exists()


def test_a_stale_lock_is_broken(pins_file):
    lock = pins_file.parent / "pins.json.lock"
    lock.mkdir()
    old = time.time() - 60
    os.utime(lock, (old, old))
    pins.pin("aaaa1111")
    assert pins.pinned() == ["aaaa1111"]


def test_a_file_that_is_not_an_array_is_left_alone(pins_file):
    pins_file.write_text('{"x": 1}')
    with pytest.raises(ValueError):
        pins.pin("aaaa1111")
    assert pins_file.read_text() == '{"x": 1}'
    assert pins.pinned() == []


def test_a_missing_file_is_made(tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_CX_JOBS", str(tmp_path / "fresh"))
    pins.pin("aaaa1111")
    assert json.loads((tmp_path / "fresh" / "pins.json").read_text()) == ["aaaa1111"]
