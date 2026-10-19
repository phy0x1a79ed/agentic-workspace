"""``scope private``: marks a project or scope private, and listings report the mark."""

from __future__ import annotations

import json

import pytest

from awm.config import privacy
from awm.scopes import scopes
from awm.scopes.hub_adapter import HANDLERS
from awm.scopes.identity import ensure_agent, ensure_project

pytestmark = [pytest.mark.scopes]


def _call(**args):
    return HANDLERS["scope_private"](args)


def _file(ws):
    return json.loads((ws["awm_dir"] / "private.json").read_text())


def test_mark_and_unmark_a_project(scopes_workspace, seeded_scopes):
    ws = scopes_workspace
    out = _call(project="proj-a", private=True)
    path = str(ws["projects_dir"] / "proj-a")
    assert out == {"project": "proj-a", "scope": None, "private": True,
                   "marks": [{"project": "proj-a", "scope": None, "path": path}]}
    assert _file(ws) == {"version": 1, "marks": out["marks"]}
    assert privacy.hidden("proj-a", "scope-1")

    out = _call(project="proj-a", private=False)
    assert out["marks"] == [] and out["private"] is False
    assert _file(ws)["marks"] == []


def test_mark_a_scope_uses_its_worktree(scopes_workspace, seeded_scopes):
    out = _call(project="proj-a", scope="scope-1", private=True)
    worktree = seeded_scopes[0][4]
    assert out["marks"] == [{"project": "proj-a", "scope": "scope-1", "path": worktree}]
    assert privacy.hidden("proj-a", "scope-1")
    assert not privacy.hidden("proj-a", "scope-2")
    assert not privacy.hidden("proj-a")


def test_a_retired_scope_can_be_marked(scopes_workspace, seeded_scopes):
    out = _call(project="proj-b", scope="scope-3", private=True)
    assert out["private"] is True


def test_marking_twice_replaces_and_keeps_other_projects(scopes_workspace, seeded_scopes):
    _call(project="proj-a", private=True)
    _call(project="proj-b", private=True)
    _call(project="proj-a", private=True)
    assert sorted(m["project"] for m in _file(scopes_workspace)["marks"]) == ["proj-a", "proj-b"]
    out = _call(project="proj-b", private=False)
    assert out["marks"] == []
    assert [m["project"] for m in _file(scopes_workspace)["marks"]] == ["proj-a"]


def test_a_project_known_only_on_disk_can_be_marked(scopes_workspace):
    (scopes_workspace["projects_dir"] / "loose" / ".bare").mkdir(parents=True)
    assert _call(project="loose", private=True)["private"] is True


def test_a_project_known_only_to_the_db_can_be_marked(scopes_workspace):
    ensure_project("rowonly", repo_path="/nowhere/.bare")
    out = _call(project="rowonly", private=True)
    assert out["marks"][0]["path"] == str(scopes_workspace["projects_dir"] / "rowonly")


def test_a_main_checkout_without_a_row_can_be_marked(scopes_workspace):
    bare = scopes_workspace["projects_dir"] / "handmade" / ".bare"
    bare.mkdir(parents=True)
    (bare / "HEAD").write_text("ref: refs/heads/release\n")
    (bare.parent / "release").mkdir()
    out = _call(project="handmade", scope="release", private=True)
    assert out["marks"][0]["path"] == str(bare.parent / "release")


def test_unknown_project_or_scope_is_refused(scopes_workspace, seeded_scopes):
    with pytest.raises(FileNotFoundError, match="Project 'nope' not found"):
        _call(project="nope", private=True)
    with pytest.raises(FileNotFoundError, match="Scope 'proj-a/nope' not found"):
        _call(project="proj-a", scope="nope", private=True)
    with pytest.raises(FileNotFoundError):
        _call(project="nope", private=False)
    assert not (scopes_workspace["awm_dir"] / "private.json").exists()


def test_a_stale_mark_clears_after_its_project_is_gone(scopes_workspace):
    privacy.write_marks([{"project": "gone", "scope": None, "path": "/x/gone"}])
    assert _call(project="gone", private=False)["marks"] == []


def test_private_must_be_a_boolean(scopes_workspace, seeded_scopes):
    with pytest.raises(ValueError):
        _call(project="proj-a", private="maybe")
    assert _call(project="proj-a", private="true")["private"] is True


def test_a_broken_marks_file_is_not_overwritten(scopes_workspace, seeded_scopes):
    path = scopes_workspace["awm_dir"] / "private.json"
    path.write_text("{broken")
    with pytest.raises(privacy.MarksUnreadable):
        _call(project="proj-a", private=True)
    assert path.read_text() == "{broken"


def test_a_project_whose_slug_prefixes_another_is_refused(scopes_workspace, seeded_scopes):
    (scopes_workspace["projects_dir"] / "proj" / ".bare").mkdir(parents=True)
    with pytest.raises(ValueError, match="proj-a"):
        _call(project="proj", private=True)
    assert not (scopes_workspace["awm_dir"] / "private.json").exists()


def test_a_project_whose_slug_equals_another_is_refused(scopes_workspace, seeded_scopes):
    (scopes_workspace["projects_dir"] / "proj_a" / ".bare").mkdir(parents=True)
    with pytest.raises(ValueError, match="share its slug prefix"):
        _call(project="proj_a", private=True)


def test_a_scope_whose_slug_prefixes_a_sibling_is_refused(scopes_workspace, seeded_scopes):
    ensure_agent("proj-a", "scope-1-b", branch="feat/scope-1-b",
                 worktree=seeded_scopes[0][4] + "-b", status="active")
    with pytest.raises(ValueError, match="scope-1-b"):
        _call(project="proj-a", scope="scope-1", private=True)


def test_nested_scopes_do_not_count_as_collisions(scopes_workspace, seeded_scopes):
    ensure_agent("proj-a", "scope-1/sub", branch="feat/scope-1/sub",
                 worktree=seeded_scopes[0][4] + "/sub", status="active")
    assert _call(project="proj-a", scope="scope-1", private=True)["private"] is True
    assert _call(project="proj-b", private=True)["private"] is True


def test_search_and_resolve_report_the_mark(scopes_workspace, seeded_scopes):
    _call(project="proj-a", scope="scope-1", private=True)
    _call(project="proj-b", private=True)
    found = scopes.search_scopes(status="all")
    flags = {(s.project, s.scope): s.private for s in found.scopes}
    assert flags == {("proj-a", "scope-1"): True, ("proj-a", "scope-2"): False,
                     ("proj-b", "scope-3"): True}
    assert {p.name: p.private for p in found.projects} == {"proj-a": False, "proj-b": True}
    dumped = HANDLERS["scope_search"]({"status": "all"})
    assert all("private" in s for s in dumped["scopes"])

    assert HANDLERS["resolveScope"]({"project": "proj-a", "scope": "scope-1"})["private"] is True
    assert HANDLERS["resolveScope"]({"project": "proj-a", "scope": "scope-2"})["private"] is False


def test_the_verb_is_a_write():
    from awm.scopes.hub_adapter import API_MANIFEST
    spec = next(f for f in API_MANIFEST["functions"] if f["name"] == "scope_private")
    assert spec["effect"] == "write" and spec["tool"] == "scope_private"
    params = {p["name"]: p for p in spec["params"]}
    assert params["private"] == {**params["private"], "type": "boolean", "required": True}
    assert params["project"]["required"] and not params["scope"]["required"]


@pytest.fixture()
def peer_marks(scopes_workspace, seeded_scopes):
    (scopes_workspace["projects_dir"] / "loose" / ".bare").mkdir(parents=True)
    _call(project="proj-a", scope="scope-1", private=True)
    _call(project="proj-b", private=True)
    _call(project="loose", private=True)
    return seeded_scopes


def _search(as_, **args):
    return HANDLERS["scope_search"]({"status": "all", **args}, as_)


@pytest.mark.parametrize("as_", ["peer", "peer:altair"])
def test_a_peer_search_lists_no_private_scope_or_project(peer_marks, as_):
    out = _search(as_)
    assert [(s["project"], s["scope"]) for s in out["scopes"]] == [("proj-a", "scope-2")]
    assert [p["name"] for p in out["projects"]] == ["proj-a"]
    assert _search(as_, project="proj-b")["scopes"] == []
    assert [s["scope"] for s in _search(as_, query="scope")["scopes"]] == ["scope-2"]
    assert "proj-b" not in json.dumps(_search(as_, query="proj-b"))


@pytest.mark.parametrize("as_", [None, "user:tony"])
def test_a_local_search_still_lists_everything(peer_marks, as_):
    out = _search(as_)
    assert len(out["scopes"]) == 3
    assert {p["name"] for p in out["projects"]} == {"proj-a", "proj-b", "loose"}


def test_a_broken_marks_file_shows_a_peer_no_scope(peer_marks, scopes_workspace):
    (scopes_workspace["awm_dir"] / "private.json").write_text("{broken")
    out = _search("peer")
    assert out["scopes"] == [] and out["projects"] == []
    assert len(_search(None)["scopes"]) == 3


def test_an_unmark_restores_the_peer_view(peer_marks):
    _call(project="proj-b", private=False)
    assert ("proj-b", "scope-3") in {(s["project"], s["scope"]) for s in _search("peer")["scopes"]}


def test_a_peer_resolves_a_private_scope_as_missing(peer_marks):
    resolve, ref = HANDLERS["resolveScope"], HANDLERS["resolveRef"]
    assert resolve({"project": "proj-b", "scope": "scope-3"}, "peer") == {
        "exists": False, "project": "proj-b", "scope": "scope-3"}
    assert resolve({"project": "proj-b", "scope": "scope-3"}, None)["exists"] is True
    assert resolve({"project": "proj-a", "scope": "scope-2"}, "peer")["exists"] is True
    assert ref({"literal": "agent:proj-a/scope-1"}, "peer:altair") is None
    assert ref({"literal": "proj-a/scope-1"}, None) == {
        "kind": "agent", "project": "proj-a", "scope": "scope-1"}


def test_a_peer_heal_reports_no_private_scope(peer_marks):
    heal = HANDLERS["scope_heal"]
    peer, local = heal({"dry_run": True}, "peer"), heal({"dry_run": True}, None)
    assert [r["scope"] for r in local["scopes"]] == ["scope-1"]
    assert peer["scopes"] == [] and peer["count"] == 0
    for key in ("unregistered_worktrees", "orphaned_rows", "audit_errors"):
        assert not {r["project"] for r in peer[key]} & {"proj-b", "loose"}
    assert {r["project"] for r in local["audit_errors"]} >= {"proj-b", "loose"}
    assert heal({"project": "proj-b", "dry_run": True}, "peer")["audit_errors"] == []
