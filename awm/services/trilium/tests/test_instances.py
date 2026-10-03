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


def test_the_vault_lives_in_the_worktree_that_serves_it():
    """One project holds Trilium and the data Trilium serves, so a deploy moves
    one path. `projects/userdata/<name>` was never a candidate: every directory
    there is one person's data on one person's branch, and the vault is
    shared."""
    assert "userdata" not in VAULT.scope.parts
    assert VAULT.scope == instances.FORK_DIR
    assert VAULT.snapshots_dir.is_relative_to(VAULT.scope / "data")
    assert VAULT.notes_dir.is_relative_to(VAULT.scope / "data")


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
