"""scope_of — which scope a directory under the projects tree belongs to."""

from __future__ import annotations

import pytest

from awm.config import scope_of

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture
def projects(tmp_path):
    root = tmp_path / "projects"
    for scope in ("awm/dev", "metasmith/engine/presentation"):
        (root / scope / ".awm").mkdir(parents=True)
    (root / "awm" / "dev" / "awm" / "services").mkdir(parents=True)
    return root


def test_a_worktree_root_is_its_scope(projects):
    assert scope_of(projects / "awm" / "dev", projects) == ("awm", "dev")


def test_a_subdirectory_reads_as_the_scope_that_holds_it(projects):
    assert scope_of(projects / "awm/dev/awm/services", projects) == ("awm", "dev")


def test_a_nested_scope_keeps_its_full_name(projects):
    there = projects / "metasmith/engine/presentation"
    assert scope_of(there, projects) == ("metasmith", "engine/presentation")


def test_a_directory_under_no_scope_falls_back_to_its_path(projects):
    (projects / "loose" / "thing").mkdir(parents=True)
    assert scope_of(projects / "loose" / "thing", projects) == ("loose", "thing")
    assert scope_of(projects / "loose", projects) == ("loose", None)


@pytest.mark.parametrize("cwd", [None, "", "/tmp"])
def test_outside_the_tree_there_is_no_scope(projects, cwd):
    assert scope_of(cwd, projects) == (None, None)


def test_the_projects_root_itself_is_no_scope(projects):
    assert scope_of(projects, projects) == (None, None)
