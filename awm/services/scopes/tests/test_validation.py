"""Tests for awm.scopes._validation and its application at each scopes entry
point that joins a user-supplied name into a filesystem path.

The guarantee under test is *containment*: a validated name, joined onto the
workspace layout, cannot escape it. Scope names may nest — ``fabfos/dev`` is a
scope at ``projects/metasmith/fabfos/dev`` — so ``/`` is a separator there and
each segment is validated as a flat name would be. Project names still reject
``/`` outright: a slashed project implies a second bare repo one level down.

(The artifacts-service entry point for the same guard lives in the artifacts
dist tests; artifacts is its own dist with its own ``_validate_name``.)
"""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.scopes, pytest.mark.smoke]

import pytest

from awm.scopes.models import (
    ProjectCreateRequest,
    ScopeCreateRequest,
    ScopeUpdateRequest,
)
from awm.scopes import projects, scopes
from awm.scopes._validation import validate_name, validate_scope_name


class TestValidateName:
    """Flat names — the form project names always take."""

    @pytest.mark.parametrize("bad", ["", ".", "..", ".hidden",
                                      "with/slash", "with\\backslash",
                                      "with\x00null"])
    def test_rejects(self, bad):
        with pytest.raises(ValueError):
            validate_name(bad, kind="name")

    @pytest.mark.parametrize("good", ["foo", "foo-bar", "foo_bar",
                                       "foo.bar", "Foo123"])
    def test_accepts(self, good):
        assert validate_name(good, kind="name") == good


class TestValidateScopeName:
    """Nested names. Every traversal case stays rejected — what changes is that
    ``/`` between two well-formed segments is now a separator, not an escape."""

    @pytest.mark.parametrize("bad", [
        "", ".", "..", ".hidden",
        "with\\backslash", "with\x00null",
        "/leading", "trailing/", "double//slash",
        "a/..", "../a", "a/../b", "a/./b",
        "a/.hidden", ".hidden/a",
    ])
    def test_rejects(self, bad):
        with pytest.raises(ValueError):
            validate_scope_name(bad)

    @pytest.mark.parametrize("good", [
        "dev", "fabfos/dev", "libraries/mono", "aspire/release",
        "a/b/c", "foo.bar/baz",
    ])
    def test_accepts(self, good):
        assert validate_scope_name(good) == good

    def test_no_nested_name_escapes_its_project(self, tmp_path):
        """The containment guarantee, stated as the property rather than as a
        list of spellings: joining any accepted name stays under the root."""
        root = tmp_path / "projects" / "proj"
        for name in ("dev", "fabfos/dev", "a/b/c"):
            joined = (root / validate_scope_name(name)).resolve()
            assert joined.is_relative_to(root.resolve())


class TestApplyAtEntryPoints:
    """Every entry point that joins a user-supplied name into a path rejects a
    project name with a separator, and accepts a well-formed nested scope."""

    def test_create_project(self, scopes_workspace):
        with pytest.raises(ValueError):
            projects.create_project(ProjectCreateRequest(name="foo/bar"))

    def test_create_scope(self, scopes_workspace):
        with pytest.raises(ValueError):
            scopes.create_scope(
                ScopeCreateRequest(project="proj/sub", scope="scope")
            )
        with pytest.raises(ValueError):
            scopes.create_scope(
                ScopeCreateRequest(project="proj", scope="../escape")
            )

    def test_update_scope(self, scopes_workspace):
        with pytest.raises(ValueError):
            scopes.update_scope("proj/sub", "scope", ScopeUpdateRequest())
        with pytest.raises(ValueError):
            scopes.update_scope("proj", "scope/..", ScopeUpdateRequest())

    def test_delete_scope(self, scopes_workspace):
        with pytest.raises(ValueError):
            scopes.delete_scope("proj/sub", "scope")
        with pytest.raises(ValueError):
            scopes.delete_scope("proj", "../scope")

    def test_refresh_history(self, scopes_workspace):
        with pytest.raises(ValueError):
            scopes.refresh_history("proj/sub", "scope")
        with pytest.raises(ValueError):
            scopes.refresh_history("proj", "scope/")

    def test_awm_refresh(self, scopes_workspace):
        with pytest.raises(ValueError):
            scopes.awm_refresh("proj", "/nested/scope")
