"""The schedule that keeps a copy a deletion cannot follow.

Every node now runs the same document, so a note deleted anywhere is gone
everywhere within a minute. The replica is the redundancy; this is the
recovery, and the two are not substitutes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from awm.trilium import vault
from awm.trilium.instances import Vault

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def scope(tmp_path):
    """A vault whose scope is a checkout, with an empty snapshot chunk."""
    (tmp_path / ".git").write_text("gitdir: elsewhere\n")
    v = Vault(scope=tmp_path)
    v.snapshots_dir.mkdir(parents=True)
    return v


def _write(v: Vault, taken: datetime, label: str = "nightly"):
    stamp = taken.strftime("%Y%m%dT%H%M%SZ")
    path = v.snapshots_dir / f"backup-{label}-{stamp}.db"
    path.write_bytes(b"x")
    return path


def test_the_name_says_when_a_snapshot_was_taken(scope):
    """A snapshot carried from another machine keeps the moment it records and
    loses the moment it arrived, so the name outranks the mtime."""
    path = _write(scope, NOW - timedelta(days=30))
    assert vault._taken_at(path) == NOW - timedelta(days=30)


def test_recent_snapshots_are_all_kept(scope):
    for days in range(0, 10):
        _write(scope, NOW - timedelta(days=days))
    assert vault.prune_snapshots(scope, keep_days=14, now=NOW) == []
    assert len(list(scope.snapshots_dir.iterdir())) == 10


def test_older_snapshots_thin_to_one_a_month(scope):
    """The working tree must not grow without bound. The archive keeps
    everything, which is what makes thinning the tree safe."""
    for day in (1, 5, 9, 20, 28):
        _write(scope, datetime(2026, 6, day, 3, 0, tzinfo=timezone.utc))
    for day in (2, 14):
        _write(scope, datetime(2026, 7, day, 3, 0, tzinfo=timezone.utc))
    kept_recent = _write(scope, NOW - timedelta(days=1))

    removed = vault.prune_snapshots(scope, keep_days=14, now=NOW)

    survivors = sorted(p.name for p in scope.snapshots_dir.iterdir())
    assert len(removed) == 5
    assert kept_recent.name in survivors
    # The newest of each month is the one that survives it.
    assert "backup-nightly-20260628T030000Z.db" in survivors
    assert "backup-nightly-20260714T030000Z.db" in survivors


def test_a_fresh_snapshot_means_no_new_one(scope):
    _write(scope, datetime.now(timezone.utc) - timedelta(hours=2))
    assert vault.scheduled_snapshot(scope)["action"] == "not-due"


def test_a_vault_with_no_snapshots_is_due(scope, monkeypatch):
    """The first tick on a node that has never snapshotted must take one, not
    wait for an age it cannot measure."""
    taken = {}
    monkeypatch.setattr(vault, "snapshot",
                        lambda v, name, commit: taken.setdefault(
                            "call", {"snapshot": f"backup-{name}", "bytes": 1}))
    monkeypatch.setattr(vault, "_pin_and_commit",
                        lambda v, message, chunk: {"committed": True,
                                                   "message": message})
    out = vault.scheduled_snapshot(scope)
    assert out["action"] == "snapshot"
    assert taken["call"]["snapshot"] == "backup-nightly"


def test_a_node_with_no_checkout_does_not_schedule(tmp_path):
    """Sirius and capella serve the published tarball and have nothing to pin.
    The schedule stands down rather than writing copies nobody ships."""
    v = Vault(scope=tmp_path)
    v.snapshots_dir.mkdir(parents=True)
    assert vault.scheduled_snapshot(v)["action"] == "not-a-checkout"


def test_the_prune_and_the_pin_land_together(scope, monkeypatch):
    """A pin committed before the prune would describe a directory somebody is
    about to thin, and the next commit would carry an unexplained deletion."""
    _write(scope, datetime(2026, 6, 1, 3, 0, tzinfo=timezone.utc))
    _write(scope, datetime(2026, 6, 20, 3, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(vault, "snapshot",
                        lambda v, name, commit: {"snapshot": "backup-nightly-x",
                                                 "bytes": 1})
    seen = {}
    monkeypatch.setattr(vault, "_pin_and_commit",
                        lambda v, message, chunk: seen.setdefault(
                            "message", message) and None or {"committed": True})
    out = vault.scheduled_snapshot(scope)
    assert out["pruned"] == ["backup-nightly-20260601T030000Z"]
    assert "pruned 1" in seen["message"]
