"""Where the vault's content lives, and which of it a pin may reach.

Slot allocation and user discovery used to be the subject here. They are gone
with the per-person design: there is one vault, on one port, in one worktree.
What survives is the layout, and it is load-bearing in a way that fails quietly
— a pin that reaches the live database looks healthy right up until someone
restores it.
"""

from __future__ import annotations

import pytest

from awm.trilium import instances

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

VAULT = instances.VAULT


def test_the_live_database_is_never_where_a_pin_would_reach():
    """The database and its write-ahead log are one logical unit, so a pin
    taken while the server runs records a state that never existed."""
    assert VAULT.document_db.is_relative_to(VAULT.data_dir)
    assert not VAULT.data_dir.is_relative_to(VAULT.snapshots_dir)
    assert VAULT.snapshots_dir.parent.parts[-2:] == ("data", "vault")
    assert VAULT.data_dir.name == "live"
    assert not VAULT.data_dir.is_relative_to(VAULT.scope / "data")


def test_trilium_never_writes_into_the_pinned_chunk():
    """`dvc add` leaves every pinned file a read-only hardlink into the shared
    cache. Trilium rewrites `backup-daily.db` in place, so its rolling
    directory has to sit outside the chunk or the daily backup stops."""
    assert VAULT.snapshots_dir not in VAULT.rolling_dir.parents
    assert VAULT.rolling_dir.is_relative_to(VAULT.data_dir)


def test_the_server_is_the_pinned_fork_release(tmp_path, monkeypatch):
    """The supervisor runs only what install.sh unpacked, with the Node the
    release carries, and status says whether that is the release awm pins."""
    server = tmp_path / "server"
    monkeypatch.setattr(instances, "SERVER_DIR", server)
    monkeypatch.setattr(instances, "SERVER_ENTRY", server / "main.cjs")
    monkeypatch.setattr(instances, "SERVER_NODE", server / "node" / "bin" / "node")
    monkeypatch.setattr(instances, "SERVER_STAMP", tmp_path / "tarball-stamp")
    monkeypatch.setattr(instances, "SERVER_PIN", tmp_path / "server.pin")
    instances.SERVER_PIN.write_text("# a comment\nversion=v2\nfile=s.tar.xz\nsha256=bb\n")
    assert instances.entry_point() is None
    assert instances.source_state() == {"entry": None, "installed": None,
                                        "pinned": "v2", "current": False}

    (server / "node" / "bin").mkdir(parents=True)
    (server / "main.cjs").write_text("")
    (server / "node" / "bin" / "node").write_text("")
    instances.SERVER_STAMP.write_text("v1 sha256:aa\n")
    assert instances.entry_point() == server / "main.cjs"
    assert instances.node_exe() == str(server / "node" / "bin" / "node")
    assert instances.source_state()["current"] is False

    instances.SERVER_STAMP.write_text("v2 sha256:bb\n")
    assert instances.source_state() == {"entry": str(server / "main.cjs"), "installed": "v2",
                                        "pinned": "v2", "current": True}


def test_install_takes_the_pushed_file_and_never_fetches():
    """mizar pushes the release to the node (install.md R115 item 4). An
    install that downloads or builds would reach past what the deploy pinned."""
    script = (instances.SERVICE_DIR / "install.sh").read_text()
    assert "/artifacts" in script and "sha256sum" in script
    for gone in ("curl", "wget", "gh release", "pnpm", "TRILIUM_INSTALL_MODE"):
        assert gone not in script
