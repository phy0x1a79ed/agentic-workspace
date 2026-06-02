"""Tests for awm.services.projects helpers."""

import pytest
pytestmark = [pytest.mark.scopes, pytest.mark.slow]

from pathlib import Path

import awm.services.projects as projects_mod
from awm.services.projects import _find_template


def test_find_template_matches_by_stem(tmp_path, monkeypatch):
    """_find_template locates a template by filename substring."""
    skills = tmp_path / "skills"
    templates = skills / "templates"
    templates.mkdir(parents=True)
    target = templates / "project-agents.md.template"
    target.write_text("hello {project}")

    monkeypatch.setattr("awm.services.projects.WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr("awm.services.projects.SKILLS_DIR", skills)

    assert _find_template("project-agents") == target


def test_find_template_returns_none_when_missing(tmp_path, monkeypatch):
    """_find_template returns None when no template matches."""
    skills = tmp_path / "skills"
    (skills / "templates").mkdir(parents=True)

    monkeypatch.setattr("awm.services.projects.WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr("awm.services.projects.SKILLS_DIR", skills)

    assert _find_template("nonexistent") is None


def test_find_template_is_layout_independent(tmp_path, monkeypatch):
    """Template can live in any subdirectory under the skills root."""
    skills = tmp_path / "skills"
    nested = skills / "some" / "other" / "place"
    nested.mkdir(parents=True)
    target = nested / "scope-agents.md.template"
    target.write_text("scope template")

    monkeypatch.setattr("awm.services.projects.WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr("awm.services.projects.SKILLS_DIR", skills)

    assert _find_template("scope-agents") == target


def test_find_template_workspace_overrides_package(tmp_path, monkeypatch):
    """Workspace skills dir takes precedence over package SKILLS_DIR."""
    workspace_skills = tmp_path / "skills"
    package_skills = tmp_path / "pkg_skills"
    (workspace_skills / "templates").mkdir(parents=True)
    (package_skills / "templates").mkdir(parents=True)

    override = workspace_skills / "templates" / "project-agents.md.template"
    override.write_text("override")
    package = package_skills / "templates" / "project-agents.md.template"
    package.write_text("package")

    monkeypatch.setattr("awm.services.projects.WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr("awm.services.projects.SKILLS_DIR", package_skills)

    assert _find_template("project-agents") == override
