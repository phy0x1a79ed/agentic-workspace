"""Pinning the persona sessions and removing the ones they replace."""

from __future__ import annotations

import json
import os
import time

import pytest

from awm.reporting import pins, reconcile

from conftest import FakePins
from stubs import FakeCx, personas, session_row

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture
def pins_file(tmp_path):
    path = tmp_path / "claude" / "jobs" / "pins.json"
    path.parent.mkdir(parents=True)
    path.write_text("[]")
    return path


async def test_pin_and_unpin_edit_the_json_array_under_the_cli_lock(pins_file):
    await pins.Pins().pin("aaaa1111")
    await pins.Pins().pin("bbbb2222")
    await pins.Pins().pin("aaaa1111")
    assert json.loads(pins_file.read_text()) == ["aaaa1111", "bbbb2222"]
    pins._edit("aaaa1111", False)
    assert json.loads(pins_file.read_text()) == ["bbbb2222"]
    assert not (pins_file.parent / "pins.json.lock").exists()


def test_a_stale_lock_is_broken_and_a_fresh_one_is_waited_for(pins_file):
    lock = pins_file.parent / "pins.json.lock"
    lock.mkdir()
    old = time.time() - 60
    os.utime(lock, (old, old))
    pins._edit("aaaa1111", True)
    assert json.loads(pins_file.read_text()) == ["aaaa1111"]


def test_a_file_that_is_not_an_array_is_left_alone(pins_file):
    pins_file.write_text('{"x": 1}')
    with pytest.raises(ValueError):
        pins._edit("aaaa1111", True)
    assert pins_file.read_text() == '{"x": 1}'


def test_a_missing_file_is_made(tmp_path):
    path = tmp_path / "fresh" / "pins.json"
    pins._edit("aaaa1111", True, path)
    assert json.loads(path.read_text()) == ["aaaa1111"]


async def test_release_unpins_and_runs_claude_rm(pins_file, tmp_path, monkeypatch):
    log = tmp_path / "rm.log"
    fake = tmp_path / "fakeclaude"
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\n")
    fake.chmod(0o755)
    monkeypatch.setattr(pins.launch, "default_claude_bin", lambda: str(fake))
    await pins.Pins().pin("aaaa1111")
    await pins.Pins().release("aaaa1111")
    assert json.loads(pins_file.read_text()) == [] and log.read_text().strip() == "rm aaaa1111"


async def test_a_failing_rm_is_logged_and_never_raised(pins_file, tmp_path, monkeypatch, caplog):
    fake = tmp_path / "fakeclaude"
    fake.write_text("#!/bin/sh\necho nope\nexit 3\n")
    fake.chmod(0o755)
    monkeypatch.setattr(pins.launch, "default_claude_bin", lambda: str(fake))
    await pins.Pins().release("aaaa1111")
    assert "claude rm aaaa1111" in caplog.text and "nope" in caplog.text


# -- the reconcile loop's use of it -------------------------------------------


def loop(cx, queue, fake):
    return reconcile.Loop(queue, cx=cx, personas=personas(), pins=fake)


async def test_a_started_session_is_pinned(queue):
    fake = FakePins()
    cx = FakeCx()
    await loop(cx, queue, fake).tick()
    assert sorted(fake.pinned) == sorted(r["job"] for r in cx.rows) and len(fake.pinned) == 2
    assert fake.released == []


async def test_sessions_the_daemon_retired_are_removed_when_their_replacement_starts(queue):
    queue.record_session("representative", "old00001")
    queue.record_session("secretary", "sec00001")
    cx = FakeCx([session_row("representative", job="old00001", state="gone"),
                 session_row("secretary", job="sec00001", state="gone"),
                 session_row("representative", job="stranger", state="gone")])
    fake = FakePins()
    await loop(cx, queue, fake).tick()
    assert sorted(fake.released) == ["old00001", "sec00001"]


async def test_a_stopped_session_cx_no_longer_lists_is_removed_when_replaced(queue):
    queue.record_session("representative", "stop0001")
    queue.record_session("representative", "long0001")
    queue.record_session("secretary", "sec00001")
    cx = FakeCx([session_row("secretary", job="sec00001")])
    fake = FakePins()
    fake.forgotten.add("long0001")
    await loop(cx, queue, fake).tick()
    assert fake.released == ["stop0001"] and len(fake.pinned) == 1


async def test_a_failed_session_that_is_replaced_is_removed(queue):
    queue.record_session("representative", "bad00001")
    queue.record_session("secretary", "sec00001")
    cx = FakeCx([session_row("representative", job="bad00001", state="failed", needs="API error"),
                 session_row("secretary", job="sec00001")])
    fake = FakePins()
    await loop(cx, queue, fake).tick()
    assert fake.released == ["bad00001"] and len(fake.pinned) == 1


async def test_nothing_is_removed_while_the_sessions_are_alive(queue):
    queue.record_session("representative", "rep00001")
    queue.record_session("secretary", "sec00001")
    cx = FakeCx([session_row("representative", job="rep00001"), session_row("secretary", job="sec00001")])
    fake = FakePins()
    await loop(cx, queue, fake).tick()
    assert fake.released == [] and fake.pinned == []
