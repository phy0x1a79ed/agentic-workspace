"""The sync carries the cache and only the cache, and never deletes on chinook.

The single-flight guard used to live here and is now a partial unique index in
the run table — those cases moved to ``test_jobs.py``. What stays is the part
that is genuinely this module's: what it puts in the transfer document, and the
append-only flags it puts on it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from awm.dvc import sync
from awm.dvc.config import SHARED_CACHE, WORKSPACE_ROOT, ChinookConfig

CFG = ChinookConfig(
    local_endpoint="local-uuid",
    remote_endpoint="remote-uuid",
    prefix="/Workspace_backups/someone/somenode",
    globus_bin="/bin/false",
)


@pytest.fixture
def submitted(monkeypatch, tmp_path):
    """A sync wired to a real-but-temporary cache, capturing what it submits."""
    cache = tmp_path / "data" / ".dvc_cache"
    cache.mkdir(parents=True)
    monkeypatch.setattr(sync, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(sync, "SHARED_CACHE", cache)
    monkeypatch.setattr("awm.dvc.config.load", lambda: CFG)

    captured: dict = {}

    def fake_submit(cfg, items, **kwargs):
        captured.update(kwargs, items=items)
        return "task-123"

    monkeypatch.setattr(sync.globus, "submit", fake_submit)
    return captured


def test_the_whole_transfer_is_one_recursive_item_for_the_shared_cache():
    items = sync.build_items(CFG)

    assert len(items) == 1
    item = items[0]
    assert item["source_path"] == f"{SHARED_CACHE}/"
    assert item["destination_path"] == f"{CFG.prefix}/data/.dvc_cache/"
    assert item["recursive"] is True


def test_the_destination_is_where_pull_reads_objects_back_from():
    # transfer.transfer_items addresses `{prefix}/{cache relative to workspace}/
    # files/md5/...`. If these two ever disagree, a restore finds nothing and
    # fails silently rather than loudly.
    from awm.dvc import transfer

    item = sync.build_items(CFG)[0]
    obj = transfer.transfer_items({"ab" + "c" * 30}, SHARED_CACHE, CFG, direction="pull")[0]

    assert obj["source_path"].startswith(item["destination_path"])


def test_the_cache_lives_inside_the_workspace_so_it_has_a_remote_path_at_all():
    assert SHARED_CACHE.relative_to(WORKSPACE_ROOT) == Path("data/.dvc_cache")


def test_sync_never_deletes_on_the_remote(submitted):
    """The one property separating a remote from a mirror.

    Asserted rather than commented because the failure mode is invisible: a
    ``delete_destination_extra: true`` sync succeeds, looks identical in every
    report, and quietly makes the next ``dvc gc`` permanent.
    """
    result = sync.sync()

    assert result["task_id"] == "task-123"
    assert submitted["delete_destination_extra"] is False
    # Content-addressed objects are immutable, so same-size is the right file.
    assert submitted["sync_level"] == 1
    # A scan of the cache races concurrent `dvc add`/`gc`; one vanished object
    # must not fail the backup.
    assert submitted["skip_source_errors"] is True
    assert len(submitted["items"]) == 1


def test_a_dry_run_submits_nothing_and_says_what_it_would_do(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("dry run must not submit")

    monkeypatch.setattr(sync.globus, "submit", explode)
    monkeypatch.setattr("awm.dvc.config.load", lambda: CFG)

    result = sync.sync(dry_run=True)

    assert result["dry_run"] is True
    assert result["append_only"] is True
    assert len(result["transfer_items"]) == 1


def test_a_missing_cache_is_an_error_not_an_empty_backup(submitted, monkeypatch, tmp_path):
    """Syncing nothing to an append-only remote succeeds and reports nothing wrong."""
    monkeypatch.setattr(sync, "SHARED_CACHE", tmp_path / "gone")

    with pytest.raises(FileNotFoundError):
        sync.sync()
