"""The vault's nightly copy: consistent copy, checks, holds and rotation (R105 items 2-3)."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from awm.trilium import backup

pytestmark = [pytest.mark.unit]

DAY = 86400


def _vault(path: Path, notes: int = 200, *, blob: str = "b", deleted=(), erased=(),
           tokens=("t1",), scripts=None, changes: int = 10) -> Path:
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE notes (noteId TEXT PRIMARY KEY, blobId TEXT, isDeleted INT, type TEXT, mime TEXT);
        CREATE TABLE etapi_tokens (etapiTokenId TEXT, isDeleted INT);
        CREATE TABLE entity_changes (id INTEGER PRIMARY KEY);
    """)
    for i in range(notes):
        nid = f"n{i}"
        if nid in erased:
            continue
        con.execute("INSERT INTO notes VALUES (?,?,?,?,?)",
                    (nid, f"{blob}{i}" if isinstance(blob, str) else blob(i), int(nid in deleted), "text", "text/html"))
    for nid, b in (scripts or {}).items():
        con.execute("INSERT INTO notes VALUES (?,?,0,'code','application/javascript;env=frontend')", (nid, b))
    con.executemany("INSERT INTO etapi_tokens VALUES (?,0)", [(t,) for t in tokens])
    con.executemany("INSERT INTO entity_changes (id) VALUES (?)", [(i,) for i in range(1, changes + 1)])
    con.commit()
    con.close()
    return path


@pytest.fixture()
def store(tmp_path):
    if not shutil.which("zstd"):
        pytest.skip("zstd not installed")
    t = {"now": 1_800_000_000.0}
    s = backup.Store(tmp_path / "store", clock=lambda: t["now"])
    s.t = t
    return s


def test_consistent_copy_reads_the_live_db(tmp_path):
    src = _vault(tmp_path / "live.db")
    dst = backup.consistent_copy(src, tmp_path / "copy.db")
    assert backup.read_facts(dst).live == backup.read_facts(src).live


def test_a_quiet_night_rotates_in(store, tmp_path):
    assert not store.accept(_vault(tmp_path / "a.db")).hold
    v = store.accept(_vault(tmp_path / "b.db", changes=20))
    assert not v.hold and v.changes == 10
    assert (store.root / "daily.db.zst").exists()


@pytest.mark.parametrize("night, reason", [
    (dict(deleted=[f"n{i}" for i in range(30)]), "notes are gone"),
    (dict(blob=lambda i: f"x{i}" if i < 120 else f"b{i}"), "changed content"),
    (dict(changes=6000), "entity changes"),
    (dict(erased=["n1"]), "erased without a soft delete"),
    (dict(tokens=("t1", "t2")), "API token"),
    (dict(scripts={"s1": "new"}), "script note"),
])
def test_each_check_holds_the_night(store, tmp_path, night, reason):
    store.accept(_vault(tmp_path / "base.db"))
    before = (store.root / "approved.db").read_bytes()
    v = store.accept(_vault(tmp_path / "night.db", **night))
    assert v.hold and any(reason in r for r in v.reasons), v.reasons
    assert (store.root / "approved.db").read_bytes() == before
    assert len(store.state()["held"]) == 1


def test_a_few_soft_deletes_pass(store, tmp_path):
    store.accept(_vault(tmp_path / "base.db"))
    assert not store.accept(_vault(tmp_path / "n.db", deleted=["n1", "n2"])).hold


def test_release_makes_the_held_night_the_baseline(store, tmp_path):
    store.accept(_vault(tmp_path / "base.db"))
    store.accept(_vault(tmp_path / "n.db", tokens=("t1", "t2")))
    stamp = store.state()["held"][0]["stamp"]
    store.release(stamp)
    assert store.state()["held"] == []
    assert backup.read_facts(store.root / "approved.db").tokens == {"t1", "t2"}


def test_rotation_keeps_day_week_month(store, tmp_path):
    for day in range(40):
        store.t["now"] = 1_800_000_000.0 + day * DAY
        store.accept(_vault(tmp_path / "n.db", changes=10 + day))
    slots = store.state()["slots"]
    now = store.t["now"]
    # The weekly copy changes once a week and the monthly once a month, so each
    # is at most that old plus one week.
    assert now - slots["daily"] == 0
    assert DAY <= now - slots["weekly"] <= WEEK + DAY
    assert slots["monthly"] < slots["weekly"]
    assert now - slots["monthly"] <= 30 * DAY + WEEK + DAY
    assert sorted(p.name for p in store.root.glob("*.zst")) == ["daily.db.zst", "monthly.db.zst", "weekly.db.zst"]


WEEK = 7 * DAY


def test_nightly_files_a_hold(store, tmp_path):
    store.accept(_vault(tmp_path / "base.db"))
    held = []
    backup.nightly(lambda p: _vault(p, erased=["n3"]), store, hold=held.append)
    assert held and "erased" in held[0][0]
