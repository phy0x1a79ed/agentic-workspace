"""The service serves the vault it finds and never makes one by itself.

A vault path with no database is refused by `start` and by the supervision
loop. `init` is the one way to create a vault, and it refuses where one exists.
"""

from __future__ import annotations

import subprocess

import pytest

from awm.trilium import instances, provision, server

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture
def vault(tmp_path, monkeypatch) -> instances.Vault:
    monkeypatch.setattr(instances, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(instances, "LOG_DIR", tmp_path / "state" / "logs")
    scope = tmp_path / "vault"
    scope.mkdir()
    entry = tmp_path / "server" / "main.cjs"
    entry.parent.mkdir()
    entry.write_text("")
    monkeypatch.setattr(instances, "entry_point", lambda: entry)
    monkeypatch.setattr(instances, "node_exe", lambda: "/bin/true")
    monkeypatch.setattr(instances, "listening", lambda *a, **k: True)
    return instances.Vault(scope=scope)


@pytest.fixture
def spawned(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    class _Proc:
        pid = 4242

        def __init__(self, cmd, **kw):
            calls.append(cmd)

        def poll(self):
            return None

    monkeypatch.setattr(subprocess, "Popen", _Proc)
    return calls


@pytest.fixture
def launchbar(monkeypatch) -> list[str]:
    settled: list[str] = []
    monkeypatch.setattr(provision, "hide_day_note_launchers",
                        lambda: settled.append("settled") or {"moved": [], "failed": []})
    return settled


def test_an_empty_path_refuses_to_start(vault, spawned, monkeypatch):
    created = []
    monkeypatch.setattr(provision, "ensure_document",
                        lambda **kw: created.append(1) or {"initialized": True})
    child = server.Child(vault)
    with pytest.raises(server.NoVault, match="awm trilium init"):
        child.start()
    assert spawned == [] and created == []


def test_an_empty_file_counts_as_no_vault(vault, spawned):
    vault.data_dir.mkdir(parents=True)
    vault.document_db.write_bytes(b"")
    with pytest.raises(server.NoVault):
        server.Child(vault).start()
    assert spawned == []


def test_the_supervision_loop_does_not_make_a_vault(vault, spawned):
    out = server.Child(vault).reconcile()
    assert out["action"] == "respawn-failed"
    assert "no vault database" in out["error"]
    assert spawned == []


def test_an_existing_db_starts(vault, spawned, launchbar, monkeypatch):
    monkeypatch.setattr(provision, "ensure_document",
                        lambda **kw: pytest.fail("start must not create a vault"))
    vault.data_dir.mkdir(parents=True)
    vault.document_db.write_bytes(b"SQLite format 3\0")
    out = server.Child(vault).start()
    assert out["action"] == "started" and out["initialized"] is True
    assert len(spawned) == 1 and launchbar == ["settled"]


def test_init_creates_a_vault(vault, spawned, launchbar, monkeypatch):
    def _create(**kw):
        vault.document_db.write_bytes(b"SQLite format 3\0")
        return {"action": "created", "initialized": True}

    monkeypatch.setattr(provision, "ensure_document", _create)
    out = server.Child(vault).init()
    assert out["action"] == "initialized" and out["initialized"] is True
    assert len(spawned) == 1 and launchbar == ["settled"]


def test_init_refuses_an_existing_vault(vault, spawned, monkeypatch):
    monkeypatch.setattr(provision, "ensure_document",
                        lambda **kw: pytest.fail("init must not touch a vault"))
    vault.data_dir.mkdir(parents=True)
    vault.document_db.write_bytes(b"SQLite format 3\0")
    with pytest.raises(FileExistsError):
        server.Child(vault).init()
    assert spawned == []
