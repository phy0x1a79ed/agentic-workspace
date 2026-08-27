"""The data lifecycle verbs, and the four ways they quietly lose a vault.

Every test here encodes a failure that looks like success at the time: a
restored database the server cannot write to, a snapshot swapped in under a
live SQLite connection, a `notes/` tree replaced with nothing, and a zip entry
that writes outside the scope.
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pytest

from awm.trilium import instances, vault

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture
def inst(tmp_path, monkeypatch) -> instances.Instance:
    users = tmp_path / "userdata"
    (users / "tony").mkdir(parents=True)
    (users / "tony" / ".git").write_text("gitdir: elsewhere\n")
    monkeypatch.setattr(instances, "USERDATA_DIR", users)
    monkeypatch.setattr(instances, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(instances, "PORTS_FILE", tmp_path / "state" / "ports.json")
    it = instances.instances()[0]
    it.data_dir.mkdir(parents=True, exist_ok=True)
    it.rolling_dir.mkdir(parents=True, exist_ok=True)
    it.snapshots_dir.mkdir(parents=True, exist_ok=True)
    return it


def _pinned(inst, name: str, body: bytes = b"sqlite", mode: int = 0o444) -> Path:
    """A snapshot as DVC leaves it: read-only, because it is a hardlink into a
    cache every other scope and every historical commit reads through."""
    path = inst.snapshots_dir / f"backup-{name}.db"
    path.write_bytes(body)
    os.chmod(path, mode)
    return path


def test_a_restored_database_is_writable(inst, monkeypatch):
    """`copy2` carries the source's mode across, and a pinned snapshot is 0444.
    A read-only `document.db` starts a server that cannot save a note."""
    monkeypatch.setattr(instances, "listening", lambda *a, **k: False)
    src = _pinned(inst, "before-upgrade-20260826T120000Z")

    vault.restore_files(inst, src)

    assert os.access(inst.document_db, os.W_OK)
    assert inst.document_db.read_bytes() == b"sqlite"


def test_a_restore_moves_the_old_vault_aside_rather_than_deleting_it(inst, monkeypatch):
    monkeypatch.setattr(instances, "listening", lambda *a, **k: False)
    for suffix in ("", "-wal", "-shm"):
        Path(str(inst.document_db) + suffix).write_bytes(b"current" + suffix.encode())
    src = _pinned(inst, "older-20260101T000000Z")

    report = vault.restore_files(inst, src)

    held = Path(report["superseded"])
    assert sorted(p.name for p in held.iterdir()) == [
        "document.db", "document.db-shm", "document.db-wal"]
    assert (held / "document.db").read_bytes() == b"current"


def test_a_restore_under_a_live_server_is_refused(inst, monkeypatch):
    """Swapping the file under an open SQLite connection leaves the process
    writing to an inode nothing can reach, and reports no error until restart."""
    monkeypatch.setattr(instances, "listening", lambda *a, **k: True)
    src = _pinned(inst, "any-20260101T000000Z")

    with pytest.raises(RuntimeError, match="still listening"):
        vault.restore_files(inst, src)


def test_a_container_backup_is_refused_rather_than_copied(inst, monkeypatch):
    """A compressed or encrypted `.tnbackup` is not a database. Copied into
    place it would be a `document.db` SQLite cannot open."""
    monkeypatch.setattr(instances, "listening", lambda *a, **k: False)
    src = inst.snapshots_dir / "backup-encrypted-20260101T000000Z.tnbackup"
    src.write_bytes(b"container")

    with pytest.raises(ValueError, match="restore screen"):
        vault.restore_files(inst, src)


def test_a_snapshot_resolves_by_stem_filename_or_bare_name(inst):
    _pinned(inst, "weekly-20260826T120000Z")
    for given in ("weekly-20260826T120000Z",
                  "backup-weekly-20260826T120000Z",
                  "backup-weekly-20260826T120000Z.db"):
        assert vault.resolve_snapshot(inst, given).name == \
            "backup-weekly-20260826T120000Z.db"


def test_snapshots_separates_the_durable_from_the_rotating(inst):
    _pinned(inst, "named-20260826T120000Z")
    (inst.rolling_dir / "backup-daily.db").write_bytes(b"rotates")

    kinds = {s["name"]: s["kind"] for s in vault.snapshots(inst)["snapshots"]}

    assert kinds["backup-named-20260826T120000Z"] == "snapshot"
    assert kinds["backup-daily"] == "rolling"


def test_an_export_entry_that_escapes_the_scope_is_dropped(tmp_path):
    """A traversal entry is dropped, not sanitised: a zip carrying one is not a
    Trilium export, and guessing what it meant is how the traversal lands."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("notes/ok.md", "fine")
        zf.writestr("../escaped.md", "not fine")
        zf.writestr("/absolute.md", "not fine")

    with zipfile.ZipFile(buf) as zf:
        kept = [m.filename for m in vault._safe_members(zf)]

    assert kept == ["notes/ok.md"]


def test_an_empty_export_does_not_replace_the_notes_tree(inst, monkeypatch):
    """`notes/` is derived, so the export replaces it wholesale. An export that
    produced nothing would then delete a person's readable copy in silence."""
    inst.notes_dir.mkdir(parents=True, exist_ok=True)
    (inst.notes_dir / "kept.md").write_text("still here")

    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w"):
        pass

    class _Api:
        def export_zip(self, note_id="root", fmt="markdown"):
            return empty.getvalue()

    monkeypatch.setattr(vault.etapi, "client", lambda _i: _Api())

    with pytest.raises(RuntimeError, match="no files"):
        vault.export(inst, commit=False)

    assert (inst.notes_dir / "kept.md").read_text() == "still here"


def test_a_held_child_is_not_respawned_by_the_supervision_loop(inst):
    """The loop respawns anything not alive, once every pass. A restore that
    did not hold the child would race it: the server comes back between the
    stop and the swap, and the swap then refuses because the port is bound."""
    from awm.trilium import server

    child = server.Child(inst)
    child.stop(hold=True)
    assert child.reconcile()["action"] == "held"

    child._held = False
    assert child.reconcile()["action"] in ("respawned", "respawn-failed")
