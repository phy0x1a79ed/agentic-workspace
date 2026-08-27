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


def test_the_vault_is_a_project_not_a_persons_scope():
    """`projects/userdata/<name>` means one person's data on one person's
    branch, and every directory in that tree is taken for a user. A shared
    knowledge base is neither, so it has its own project and its own history."""
    parts = VAULT.scope.parts
    assert "userdata" not in parts
    assert parts[-2:] == ("vault", "main")


def test_the_live_database_is_never_where_a_pin_would_reach():
    """The database and its write-ahead log are one logical unit, so a pin
    taken while the server runs records a state that never existed."""
    assert VAULT.document_db.is_relative_to(VAULT.data_dir)
    assert not VAULT.data_dir.is_relative_to(VAULT.snapshots_dir)
    assert VAULT.snapshots_dir.parent.name == "data"
    assert VAULT.data_dir.name == "live"


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
