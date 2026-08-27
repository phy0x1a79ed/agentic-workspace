"""Port allocation and user discovery.

Both are load-bearing in a way that fails quietly. A user's port is in their
browser's bookmark, so an allocation that renumbers on an unrelated change
moves a live URL with no error anywhere. And the two port bands sit ten apart,
so an unbounded user count silently walks the upstream band into the next
service's front band.
"""

from __future__ import annotations

import pytest

from awm.trilium import instances

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    users = tmp_path / "userdata"
    users.mkdir()
    monkeypatch.setattr(instances, "USERDATA_DIR", users)
    monkeypatch.setattr(instances, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(instances, "PORTS_FILE", tmp_path / "state" / "ports.json")
    return users


def _add(users, name):
    scope = users / name
    scope.mkdir()
    (scope / ".git").write_text("gitdir: elsewhere\n")
    return scope


def test_a_scope_on_disk_is_the_whole_of_adding_a_user(workspace):
    assert instances.discovered_users() == []
    _add(workspace, "tony")
    assert instances.discovered_users() == ["tony"]


def test_a_directory_without_a_git_is_not_a_user(workspace):
    (workspace / "notes-backup").mkdir()
    assert instances.discovered_users() == []


def test_a_name_that_cannot_be_a_port_key_is_refused(workspace):
    _add(workspace, "Tony.Liu")
    assert instances.discovered_users() == []


def test_an_existing_user_keeps_their_port_when_someone_sorts_before_them(workspace):
    """The bug this guards: deriving a slot from sorted position renumbers
    everyone after an insertion, moving a URL somebody has bookmarked."""
    _add(workspace, "tony")
    before = {i.user: i.front_port for i in instances.instances()}
    _add(workspace, "alice")
    after = {i.user: i.front_port for i in instances.instances()}
    assert after["tony"] == before["tony"]
    assert after["alice"] != after["tony"]


def test_the_two_bands_never_overlap(workspace):
    for n in range(instances.MAX_USERS):
        _add(workspace, f"u{n}")
    live = instances.instances()
    assert len(live) == instances.MAX_USERS
    fronts = {i.front_port for i in live}
    upstreams = {i.upstream_port for i in live}
    assert not (fronts & upstreams)


def test_users_beyond_the_band_are_dropped_rather_than_collided(workspace):
    for n in range(instances.MAX_USERS + 3):
        _add(workspace, f"u{n:02d}")
    assert len(instances.instances()) == instances.MAX_USERS


def test_the_live_database_is_never_where_a_pin_would_reach(workspace):
    """`live/` is gitignored by the userdata template and `data/backups` is the
    chunk. They must not be the same directory, or a pin would capture a
    database that is being written to."""
    _add(workspace, "tony")
    inst = instances.instances()[0]
    assert inst.snapshots_dir != inst.data_dir
    assert inst.snapshots_dir.parent.name == "data"
    assert inst.data_dir.name == "live"


def test_trilium_never_writes_into_the_pinned_chunk(workspace):
    """`dvc add` leaves every pinned file a read-only hardlink into the shared
    cache. Trilium rewrites `backup-daily.db` in place, so its rolling
    directory has to sit outside the chunk or the daily backup stops."""
    _add(workspace, "tony")
    inst = instances.instances()[0]
    assert inst.snapshots_dir not in inst.rolling_dir.parents
    assert inst.rolling_dir.is_relative_to(inst.data_dir)


def test_a_host_that_cannot_hold_a_checkout_still_has_users(workspace, monkeypatch):
    """sirius holds no GitHub credential and its `projects/` is a symlink into a
    state directory owned by another account, so a scope there is a plain
    directory. Requiring a `.git` would leave that host with no users at all."""
    (workspace / "tony").mkdir()
    assert instances.discovered_users() == []

    monkeypatch.setattr(instances, "REQUIRE_SCOPE_GIT", False)
    assert instances.discovered_users() == ["tony"]


def test_dropping_the_git_requirement_does_not_drop_the_name_check(workspace, monkeypatch):
    """The name is a port-allocation key and a log filename, so it is checked
    whether or not a `.git` is."""
    monkeypatch.setattr(instances, "REQUIRE_SCOPE_GIT", False)
    (workspace / "Not A User").mkdir()
    (workspace / ".hidden").mkdir()
    assert instances.discovered_users() == []


def test_install_artifacts_are_not_in_runtime_state(workspace):
    """The user that installs and the user that runs differ on sirius. Anything
    written at install time has to land where the installer can write it."""
    assert instances.TARBALL_DIR.is_relative_to(instances.INSTALL_DIR)
    assert instances.NODE_BIN_FILE.is_relative_to(instances.INSTALL_DIR)
    assert not instances.INSTALL_DIR.is_relative_to(instances.STATE_DIR)
