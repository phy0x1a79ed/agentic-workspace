"""Where the vault's content lives, and which of it a pin may reach.

Slot allocation and user discovery used to be the subject here. They are gone
with the per-person design: there is one vault, on one port, in one worktree.
What survives is the layout, and it is load-bearing in a way that fails quietly
— a pin that reaches the live database looks healthy right up until someone
restores it.
"""

from __future__ import annotations

import pytest

from awm import config
from awm.trilium import instances

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

VAULT = instances.VAULT


def test_the_vault_has_a_repository_apart_from_the_fork():
    """The fork is public and its release tags name code. Nightly pins committed
    there would publish with it and move `git describe` on every release.
    `projects/userdata/<name>` is not a candidate either: every directory there
    is one person's data, and the vault is shared."""
    assert "userdata" not in instances.VAULT_HOME.parts
    assert instances.VAULT_HOME.parts[-3:] == ("projects", "vault", "release")
    assert not instances.VAULT_HOME.is_relative_to(instances.FORK_DIR)
    assert VAULT.snapshots_dir.is_relative_to(VAULT.scope / "data")
    assert VAULT.notes_dir.is_relative_to(VAULT.scope / "data")


def test_the_default_scope_moves_only_once_the_vault_checkout_exists(
        monkeypatch, tmp_path):
    """A deploy of this code must not strand a node's database: `live/` moves
    in a host step, with the server stopped."""
    home = tmp_path / "projects" / "vault" / "release"
    monkeypatch.setattr(instances, "VAULT_HOME", home)
    assert instances._default_scope() == instances.FORK_DIR
    home.mkdir(parents=True)
    assert instances._default_scope() == home


def test_a_database_left_in_the_fork_blocks_a_start_elsewhere(
        monkeypatch, tmp_path):
    """Trilium would provision an empty vault beside the real one and report it
    initialized."""
    fork, home = tmp_path / "fork", tmp_path / "home"
    (fork / "live").mkdir(parents=True)
    home.mkdir()
    monkeypatch.setattr(instances, "FORK_DIR", fork)
    vault = instances.Vault(scope=home)
    assert instances.stranded_database(vault) is None
    (fork / "live" / "document.db").write_bytes(b"")
    assert instances.stranded_database(vault) == fork / "live" / "document.db"
    assert instances.stranded_database(instances.Vault(scope=fork)) is None
    (home / "live").mkdir()
    (home / "live" / "document.db").write_bytes(b"")
    assert instances.stranded_database(vault) is None


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


def test_a_restore_keeps_what_it_replaced_out_of_git_and_out_of_the_chunk():
    """Superseded vaults are recoverable by moving a file back, which they
    would not be if they were pinned or committed."""
    assert VAULT.superseded_dir.is_relative_to(VAULT.data_dir)


def test_install_artifacts_are_not_in_runtime_state():
    """The user that installs and the user that runs differ on sirius. Anything
    written at install time has to land where the installer can write it."""
    assert instances.TARBALL_DIR.is_relative_to(instances.INSTALL_DIR)
    assert instances.NODE_BIN_FILE.is_relative_to(instances.INSTALL_DIR)
    assert not instances.INSTALL_DIR.is_relative_to(instances.STATE_DIR)


def test_the_port_has_exactly_one_definition():
    """The supervisor binds it and the edge proxies to it. A second copy is a
    vault nobody can reach, with no error that says why."""
    assert instances.UPSTREAM_PORT is config.VAULT_PORT


def test_the_dirtiness_probe_and_the_installer_exclude_the_same_paths():
    """`install.sh` rebuilds the whole monorepo when it reads the worktree as
    dirty, and the vault writes into that worktree. The two probes have to name
    the same exclusions or a deploy alternates between rebuilding and not."""
    script = (instances.SERVICE_DIR / "install.sh").read_text()
    assert instances.NOT_SOURCE == (":!data", ":!live")
    for spec in instances.NOT_SOURCE:
        assert f"'{spec}'" in script


def test_the_scope_is_overridable_for_a_sandbox(monkeypatch):
    """A dev sandbox points at its own vault rather than sharing the host's."""
    monkeypatch.setenv("TRILIUM_VAULT_SCOPE", "/tmp/elsewhere")
    import importlib
    reloaded = importlib.reload(instances)
    try:
        assert str(reloaded.SCOPE) == "/tmp/elsewhere"
    finally:
        monkeypatch.delenv("TRILIUM_VAULT_SCOPE")
        importlib.reload(instances)


def test_a_tarball_from_the_fork_release_counts_as_the_fork(tmp_path, monkeypatch):
    server = tmp_path / "server"
    (server / "node" / "bin").mkdir(parents=True)
    (server / "main.cjs").write_text("")
    (server / "node" / "bin" / "node").write_text("")
    monkeypatch.setattr(instances, "FORK_ENTRY", tmp_path / "no-fork" / "main.cjs")
    monkeypatch.setattr(instances, "TARBALL_ENTRY", server / "main.cjs")
    monkeypatch.setattr(instances, "TARBALL_NODE", server / "node" / "bin" / "node")
    monkeypatch.setattr(instances, "TARBALL_STAMP", tmp_path / "tarball-stamp")
    instances.TARBALL_STAMP.write_text("TriliumNotes-Server-v0.105.0-linux-x64.tar.xz\n")
    assert instances.entry_point()[1] == "tarball"
    instances.TARBALL_STAMP.write_text("fork v0.105.0-awm.1 sha256:ab\n")
    assert instances.entry_point() == (server / "main.cjs", "fork")
    assert instances.node_exe() == str(server / "node" / "bin" / "node")


def test_the_zotero_mirror_resolves_the_same_vault():
    """The mirror pins its chunk into the vault's checkout. The path is spelled
    once per dist, so a default that moves here and not in zotero splits the
    vault across two repositories."""
    text = instances.SERVICE_DIR.parent.joinpath("zotero", "awm", "zotero", "sync.py").read_text()
    assert '"vault" / "release"' in text
    assert '"trilium" / "release"' in text
