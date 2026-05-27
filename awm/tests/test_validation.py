"""Tests for awm.services._validation.validate_name and its application at
each entry point that joins a user-supplied name into a filesystem path.

The bug class this guards against: ``awm_refresh scope=metasmith/dev``
joining the slashed scope literally onto the project dir and producing the
orphan ``projects/metasmith/metasmith/dev/``.
"""

from __future__ import annotations

import pytest

from awm.models import (
    ArtifactRegisterRequest,
    ProjectCreateRequest,
    ScopeCreateRequest,
    ScopeUpdateRequest,
)
from awm.services import artifacts, projects, scopes
from awm.services._validation import validate_name


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
    """Each public entry point that joins user-supplied name into a
    filesystem path must reject the slashed-scope bug."""

    def test_create_project(self, awm_workspace):
        with pytest.raises(ValueError):
            projects.create_project(ProjectCreateRequest(name="foo/bar"))

    def test_create_scope(self, awm_workspace):
        with pytest.raises(ValueError):
            scopes.create_scope(
                ScopeCreateRequest(project="proj", scope="nested/scope")
            )
        with pytest.raises(ValueError):
            scopes.create_scope(
                ScopeCreateRequest(project="proj/sub", scope="scope")
            )

    def test_update_scope(self, awm_workspace):
        with pytest.raises(ValueError):
            scopes.update_scope("proj/sub", "scope", ScopeUpdateRequest())
        with pytest.raises(ValueError):
            scopes.update_scope("proj", "scope/sub", ScopeUpdateRequest())

    def test_delete_scope(self, awm_workspace):
        with pytest.raises(ValueError):
            scopes.delete_scope("proj/sub", "scope")
        with pytest.raises(ValueError):
            scopes.delete_scope("proj", "scope/sub")

    def test_refresh_history(self, awm_workspace):
        with pytest.raises(ValueError):
            scopes.refresh_history("proj/sub", "scope")
        with pytest.raises(ValueError):
            scopes.refresh_history("proj", "scope/sub")

    def test_refresh_artifacts(self, awm_workspace):
        with pytest.raises(ValueError):
            scopes.refresh_artifacts("proj/sub", "scope")

    def test_awm_refresh(self, awm_workspace):
        with pytest.raises(ValueError):
            scopes.awm_refresh("proj", "nested/scope")

    def test_register_artifact(self, awm_workspace):
        with pytest.raises(ValueError):
            artifacts.register_artifact(
                ArtifactRegisterRequest(
                    project="proj/sub", scope="scope", name="thing",
                    artifact_type="other", path="data/foo",
                )
            )
        with pytest.raises(ValueError):
            artifacts.register_artifact(
                ArtifactRegisterRequest(
                    project="proj", scope="scope/sub", name="thing",
                    artifact_type="other", path="data/foo",
                )
            )
