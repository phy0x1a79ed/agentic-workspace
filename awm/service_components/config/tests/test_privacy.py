"""Private marks: who counts as a peer, what a mark covers, and how the file fails."""

from __future__ import annotations

import json
import os

import pytest

import awm.config as config
from awm.config import privacy

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def awm_dir(monkeypatch, tmp_path):
    d = tmp_path / ".awm"
    d.mkdir()
    monkeypatch.setattr(config, "AWM_DIR", d)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    return d


@pytest.fixture()
def projects(tmp_path):
    root = tmp_path / "projects"
    (root / "avatar" / "Avatar" / "dev").mkdir(parents=True)
    (root / "avatar2").mkdir()
    (root / "awm" / "svc-a").mkdir(parents=True)
    return root


def _mark(projects, project, scope=None):
    path = projects / project / scope if scope else projects / project
    return {"project": project, "scope": scope, "path": str(path)}


@pytest.mark.parametrize("stamp,peer", [
    ("peer", True), ("peer:capella", True), (" PEER:Altair ", True), ("Peer", True),
    ("user:tony", False), (None, False), ("", False), ("agent:awm/x", False),
    ("peerless", False), (42, False),
])
def test_from_peer(stamp, peer):
    assert privacy.from_peer(stamp) is peer


def test_missing_file_means_no_marks(projects):
    assert privacy.marks() == []
    assert not privacy.hidden("avatar")
    assert not privacy.hidden_path(projects / "avatar")
    rows = [{"project": "avatar"}]
    assert privacy.visible_rows(rows, "peer:altair") == rows


def test_project_mark_hides_project_and_nested_scopes(projects):
    privacy.write_marks([_mark(projects, "avatar")])
    assert privacy.hidden("avatar")
    assert privacy.hidden("avatar", "Avatar")
    assert privacy.hidden("avatar", "Avatar/dev")
    assert not privacy.hidden("avatar2")
    assert not privacy.hidden("awm", "svc-a")


def test_scope_mark_hides_that_scope_and_nested_ones(projects):
    privacy.write_marks([_mark(projects, "avatar", "Avatar")])
    assert privacy.hidden("avatar", "Avatar")
    assert privacy.hidden("avatar", "Avatar/dev")
    assert not privacy.hidden("avatar", "Avatar2")
    assert not privacy.hidden("avatar")
    assert not privacy.hidden("avatar", "Mate-Engine")


def test_path_prefix_is_component_wise(projects):
    privacy.write_marks([_mark(projects, "avatar")])
    assert privacy.hidden_path(projects / "avatar")
    assert privacy.hidden_path(str(projects / "avatar" / "Mate-Engine" / "x.md"))
    assert privacy.hidden_path(str(projects / "avatar" / "Avatar" / ".." / "PLAN.md"))
    assert not privacy.hidden_path(projects / "avatar2")
    assert not privacy.hidden_path(projects / "avatar2" / "x")
    assert not privacy.hidden_path(projects)
    assert not privacy.hidden_path("")


def test_relative_paths_resolve_against_the_workspace(projects):
    privacy.write_marks([_mark(projects, "avatar")])
    assert privacy.hidden_path("projects/avatar/Avatar")
    assert not privacy.hidden_path("projects/awm")


def test_path_through_a_symlink_is_hidden(projects, tmp_path):
    privacy.write_marks([_mark(projects, "avatar")])
    link = tmp_path / "shortcut"
    link.symlink_to(projects / "avatar")
    assert privacy.hidden_path(link / "Avatar")


def test_slug_rule_matches_claude_code():
    assert privacy.slug("/home/tony/agentic_workspace/projects/awm/svc-scopes") == \
        "-home-tony-agentic-workspace-projects-awm-svc-scopes"
    assert privacy.slug("/home/tony/agentic_workspace/projects/awm/.bare") == \
        "-home-tony-agentic-workspace-projects-awm--bare"


def test_slug_equality_and_prefix(projects):
    privacy.write_marks([_mark(projects, "avatar")])
    base = privacy.slug(projects / "avatar")
    assert privacy.hidden_slug(base)
    assert privacy.hidden_slug(base + "-Avatar-dev")
    assert privacy.hidden_slug(base + "--claude")
    assert not privacy.hidden_slug(privacy.slug(projects / "avatar2"))
    assert not privacy.hidden_slug(base + "2-Avatar")
    assert not privacy.hidden_slug(privacy.slug(projects / "awm"))


def test_visible_rows_filters_only_for_peers(projects):
    privacy.write_marks([_mark(projects, "avatar")])
    rows = [
        {"project": "avatar", "scope": "Avatar/dev"},
        {"project": "awm", "scope": "svc-a"},
        {"name": "loose", "cwd": str(projects / "avatar" / "Mate-Engine")},
        {"name": "tree", "worktree": str(projects / "avatar2")},
        {"repo_path": str(projects / "avatar" / ".bare")},
        "not-a-dict",
    ]
    assert privacy.visible_rows(rows, None) is rows
    assert privacy.visible_rows(rows, "user:tony") is rows
    assert privacy.visible_rows(rows, "peer:altair") == [
        {"project": "awm", "scope": "svc-a"},
        {"name": "tree", "worktree": str(projects / "avatar2")},
        "not-a-dict",
    ]
    assert privacy.visible_rows(rows, "peer") == privacy.visible_rows(rows, "peer:x")


def test_scope_mark_rows(projects):
    privacy.write_marks([_mark(projects, "avatar", "Avatar")])
    rows = [{"project": "avatar"}, {"project": "avatar", "scope": "Avatar/dev"},
            {"project": "avatar", "scope": "Mate-Engine"}]
    assert privacy.visible_rows(rows, "peer") == [rows[0], rows[2]]


def test_reloads_when_the_file_changes(projects, awm_dir):
    privacy.write_marks([_mark(projects, "avatar")])
    assert privacy.hidden("avatar")
    privacy.write_marks([_mark(projects, "awm")])
    assert not privacy.hidden("avatar")
    assert privacy.hidden("awm")
    path = awm_dir / "private.json"
    doc = json.loads(path.read_text())
    doc["marks"][0]["project"] = "aw"
    path.write_text(json.dumps(doc))
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert privacy.hidden("aw")
    path.unlink()
    assert not privacy.hidden("aw")


@pytest.mark.parametrize("body", [
    "{not json",
    "[]",
    json.dumps({"version": 2, "marks": []}),
    json.dumps({"version": 1, "marks": {}}),
    json.dumps({"version": 1, "marks": [{"project": "p"}]}),
    json.dumps({"version": 1, "marks": [{"project": "p", "scope": None, "path": "rel"}]}),
    json.dumps({"version": 1, "marks": [{"project": "", "scope": None, "path": "/x"}]}),
])
def test_a_broken_file_fails_closed(awm_dir, projects, body):
    (awm_dir / "private.json").write_text(body)
    assert privacy.hidden("anything")
    assert privacy.hidden("anything", "at/all")
    assert privacy.hidden_path("/etc")
    assert privacy.hidden_slug("-home-tony")
    assert privacy.visible_rows([{"project": "awm"}, "x"], "peer:altair") == []
    assert privacy.visible_rows([{"project": "awm"}], "user:tony") == [{"project": "awm"}]
    assert privacy.hides_args({"project": "awm"})
    assert privacy.hides_args({"path": "/tmp"})
    assert not privacy.hides_args({"query": "notes"})
    with pytest.raises(privacy.MarksUnreadable):
        privacy.marks()


def test_hides_args(projects):
    privacy.write_marks([_mark(projects, "avatar"), _mark(projects, "awm", "svc-a")])
    assert privacy.hides_args({"project": "avatar"})
    assert privacy.hides_args({"project": "avatar", "scope": "Mate-Engine"})
    assert privacy.hides_args({"project": "awm", "scope": "svc-a"})
    assert privacy.hides_args({"project": "awm", "scope": "svc-a/sub"})
    assert not privacy.hides_args({"project": "awm"})
    assert not privacy.hides_args({"project": "awm", "scope": "svc-b"})
    assert privacy.hides_args({"cwd": str(projects / "avatar" / "Avatar")})
    assert privacy.hides_args({"scope": str(projects / "awm" / "svc-a")})
    assert privacy.hides_args({"worktree": str(projects / "avatar")})
    assert privacy.hides_args({"path": str(projects / "avatar" / "x")})
    assert not privacy.hides_args({"path": str(projects / "avatar2")})
    assert not privacy.hides_args({"scope": "svc-a"})
    assert not privacy.hides_args({"query": "avatar"})
    assert not privacy.hides_args(None)
    assert not privacy.hides_args({"project": 3, "path": 4})


def test_write_marks_rejects_bad_entries_and_leaves_no_temp(awm_dir, projects):
    with pytest.raises(privacy.MarksUnreadable):
        privacy.write_marks([{"project": "p", "scope": None, "path": "relative"}])
    written = privacy.write_marks([_mark(projects, "avatar")])
    assert privacy.marks() == written
    assert json.loads((awm_dir / "private.json").read_text())["version"] == 1
    assert sorted(p.name for p in awm_dir.iterdir()) == ["private.json"]


def test_legacy_journal_spellings_name_the_project(projects):
    privacy.write_marks([_mark(projects, "avatar")])
    assert privacy.hidden("task:avatar")
    assert privacy.hidden("", "user:proj:avatar")
    assert privacy.hidden("", "project:avatar")
    assert not privacy.hidden("", "user:proj:avatar2")
    assert privacy.visible_rows([{"project": "task:avatar"}, {"project": "", "scope": "user:proj:avatar"},
                                 {"project": "", "scope": "workspace"}], "peer") == [{"project": "", "scope": "workspace"}]
    assert privacy.hides_args({"scope": "user:proj:avatar"})
    assert not privacy.hides_args({"scope": "dev"})
