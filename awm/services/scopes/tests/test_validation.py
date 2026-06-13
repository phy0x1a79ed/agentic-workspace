"""Tests for awm.scopes._validation.validate_name and its application at
each scopes entry point that joins a user-supplied name into a filesystem
path.

The bug class this guards against: ``awm_refresh scope=metasmith/dev``
joining the slashed scope literally onto the project dir and producing the
orphan ``projects/metasmith/metasmith/dev/``.

(The artifacts-service entry point for the same guard lives in the
artifacts dist tests; artifacts is its own dist with its own
``_validate_name``.)
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
from awm.scopes._validation import validate_name


class TestValidateName:
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


class TestApplyAtEntryPoints:
    """Each public scopes entry point that joins user-supplied name into a
    filesystem path must reject the slashed-scope bug."""

    def test_create_project(self, scopes_workspace):
        with pytest.raises(ValueError):
            projects.create_project(ProjectCreateRequest(name="foo/bar"))

    def test_create_scope(self, scopes_workspace):
        with pytest.raises(ValueError):
            scopes.create_scope(
                ScopeCreateRequest(project="proj", scope="nested/scope")
            )
        with pytest.raises(ValueError):
            scopes.create_scope(
                ScopeCreateRequest(project="proj/sub", scope="scope")
            )

    def test_update_scope(self, scopes_workspace):
        with pytest.raises(ValueError):
            scopes.update_scope("proj/sub", "scope", ScopeUpdateRequest())
        with pytest.raises(ValueError):
            scopes.update_scope("proj", "scope/sub", ScopeUpdateRequest())

    def test_delete_scope(self, scopes_workspace):
        with pytest.raises(ValueError):
            scopes.delete_scope("proj/sub", "scope")
        with pytest.raises(ValueError):
            scopes.delete_scope("proj", "scope/sub")

    def test_refresh_history(self, scopes_workspace):
        with pytest.raises(ValueError):
            scopes.refresh_history("proj/sub", "scope")
        with pytest.raises(ValueError):
            scopes.refresh_history("proj", "scope/sub")

    def test_refresh_artifacts(self, scopes_workspace):
        with pytest.raises(ValueError):
            scopes.refresh_artifacts("proj/sub", "scope")

    def test_awm_refresh(self, scopes_workspace):
        with pytest.raises(ValueError):
            scopes.awm_refresh("proj", "nested/scope")
