"""Fixtures captured from a live box, replayed against a temporary home.

The roster records liveness as a pid plus that process's start time, so a
captured roster is dead the moment the box moves on. The capture therefore
carries `__ALIVE_PID__` / `__ALIVE_START__` placeholders and this module binds
them to the *test process itself*, which is the one process guaranteed to be
running while the assertions execute.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "box"

#: The version the captured sessions were seeded by.
VERSION = "2.1.268"


def _own_proc_start() -> str:
    with open("/proc/self/stat") as fh:
        raw = fh.read()
    return raw.partition(") ")[2].split()[19]


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A copy of the captured box, with the live placeholders bound."""
    import os

    root = tmp_path / "claude"
    shutil.copytree(FIXTURES, root)
    roster = root / "daemon" / "roster.json"
    text = (
        roster.read_text()
        .replace('"__ALIVE_PID__"', str(os.getpid()))
        .replace('"__ALIVE_START__"', json.dumps(_own_proc_start()))
    )
    # The capture's start times age with the wall clock until every session is
    # past the rotate age. Shift them together so the newest is seconds old and
    # the spacing between sessions stays as captured.
    data = json.loads(text)
    newest = max(w["startedAt"] for w in data["workers"].values())
    shift = int(time.time() * 1000) - 5000 - newest
    for w in data["workers"].values():
        w["startedAt"] += shift
    roster.write_text(json.dumps(data))
    monkeypatch.setenv("AWM_CX_ROSTER", str(roster))
    monkeypatch.setenv("AWM_CX_JOBS", str(root / "jobs"))
    monkeypatch.setenv("AWM_CX_STATE", str(root / "cx"))
    # The pool reads the binary's version off the symlink rather than by running
    # it, so a symlink into a directory named for a version is the whole fake.
    (root / "versions" / VERSION).mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "bin" / "claude").symlink_to(root / "versions" / VERSION)
    monkeypatch.setenv("AWM_CX_CLAUDE", str(root / "bin" / "claude"))
    return root


@pytest.fixture
def by_short(box):
    """The captured sessions keyed by short id."""
    from awm.cx import sessions

    return {s.short: s for s in sessions.load()}
