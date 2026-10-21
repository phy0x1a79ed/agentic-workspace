"""``scope tag``: tags on projects, scopes and groups, resolved by nearest level."""

from __future__ import annotations

import pytest

from awm.scopes import tags
from awm.scopes.hub_adapter import HANDLERS

pytestmark = [pytest.mark.scopes]


def _tag(as_=None, **args):
    return HANDLERS["scope_tag"](args, as_)


def test_normalise_accepts_lists_json_and_comma_strings():
    assert tags.normalise(["Lab", "tony", "lab"]) == ["lab", "tony"]
    assert tags.normalise('["lab", "ana"]') == ["ana", "lab"]
    assert tags.normalise("lab, ana ,") == ["ana", "lab"]
    assert tags.normalise("") == []


@pytest.mark.parametrize("bad", ["-lab", "lab work", "a" * 33, "lab/x", 7])
def test_normalise_refuses_a_bad_tag_with_the_rule(bad):
    with pytest.raises(ValueError, match="at most 32 characters"):
        tags.normalise([bad])


def test_levels_run_nearest_first():
    assert tags.levels("lab/ana/x") == ["lab/ana/x", "lab/ana", "lab", ""]
    assert tags.levels(None) == [""]


def test_project_tags_reach_its_scopes(scopes_workspace, seeded_scopes):
    out = _tag(project="proj-a", tags=["tony"])
    assert out == {"project": "proj-a", "scope": None, "tags": ["tony"],
                   "effective": ["tony"], "effective_from": "proj-a"}
    read = _tag(project="proj-a", scope="scope-1")
    assert read["tags"] == [] and read["effective"] == ["tony"]
    assert read["effective_from"] == "proj-a"


def test_a_nearer_level_replaces_the_project_set(scopes_workspace, seeded_scopes):
    _tag(project="proj-a", tags="tony")
    _tag(project="proj-a", scope="lab", tags=["lab"])
    _tag(project="proj-a", scope="lab/ana", tags=["lab", "ana"])
    assert tags.effective("proj-a", "lab/bo") == ["lab"]
    assert tags.effective("proj-a", "lab/ana/deep") == ["ana", "lab"]
    assert tags.effective("proj-a", "scope-1") == ["tony"]
    assert tags.effective("proj-b", "scope-3") == []


def test_an_empty_set_clears_the_level(scopes_workspace, seeded_scopes):
    _tag(project="proj-a", tags=["tony"])
    _tag(project="proj-a", scope="scope-1", tags=["lab"])
    out = _tag(project="proj-a", scope="scope-1", tags=[])
    assert out["tags"] == [] and out["effective"] == ["tony"]


def test_an_unknown_project_is_refused(scopes_workspace, seeded_scopes):
    with pytest.raises(FileNotFoundError):
        _tag(project="nope", tags=["lab"])


def test_a_peer_reads_but_cannot_write(scopes_workspace, seeded_scopes):
    _tag(project="proj-a", tags=["tony"])
    assert _tag("peer:capella", project="proj-a")["tags"] == ["tony"]
    with pytest.raises(ValueError, match="peer"):
        _tag("peer:capella", project="proj-a", tags=["lab"])


def test_a_peer_cannot_read_a_private_project(scopes_workspace, seeded_scopes):
    HANDLERS["scope_private"]({"project": "proj-a", "private": True})
    with pytest.raises(FileNotFoundError):
        _tag("peer", project="proj-a")


def test_delete_drops_the_scope_level(scopes_workspace, seeded_scopes, monkeypatch):
    from awm.scopes import scopes
    _tag(project="proj-a", tags=["tony"])
    _tag(project="proj-a", scope="scope-1", tags=["lab"])
    monkeypatch.setattr(scopes, "_cleanup_worktree", lambda *a, **k: None)
    monkeypatch.setattr(scopes, "_settle_branch", lambda *a, **k: "branch kept")
    scopes.delete_scope("proj-a", "scope-1")
    assert tags.load("proj-a") == {("proj-a", ""): ["tony"]}


def _search(as_=None, **args):
    return HANDLERS["scope_search"]({"status": "all", **args}, as_)


def test_search_carries_tags_and_lists_those_in_use(scopes_workspace, seeded_scopes):
    _tag(project="proj-a", tags=["tony"])
    _tag(project="proj-a", scope="scope-2", tags=["lab", "ana"])
    out = _search()
    by_scope = {s["scope"]: s["tags"] for s in out["scopes"]}
    assert by_scope == {"scope-1": ["tony"], "scope-2": ["ana", "lab"], "scope-3": []}
    assert {p["name"]: p["tags"] for p in out["projects"]} == {"proj-a": ["tony"], "proj-b": []}
    assert out["tags"] == {"ana": 1, "lab": 1, "tony": 1}


def test_search_filters_by_tag(scopes_workspace, seeded_scopes):
    _tag(project="proj-a", tags=["tony"])
    _tag(project="proj-a", scope="scope-2", tags=["lab"])
    assert [s["scope"] for s in _search(tag="lab")["scopes"]] == ["scope-2"]
    assert [s["scope"] for s in _search(tag="Tony")["scopes"]] == ["scope-1"]
    assert _search(tag="lab")["tags"] == {"lab": 1, "tony": 1}
    assert _search(tag="collab")["scopes"] == []


def test_search_in_one_project_lists_no_tags(scopes_workspace, seeded_scopes):
    _tag(project="proj-a", tags=["tony"])
    out = _search(project="proj-a")
    assert "tags" not in out and out["scopes"][0]["tags"] == ["tony"]


def test_resolve_scope_returns_effective_tags(scopes_workspace, seeded_scopes):
    _tag(project="proj-a", tags=["tony"])
    out = HANDLERS["resolveScope"]({"project": "proj-a", "scope": "scope-1"})
    assert out["tags"] == ["tony"]


def test_create_refuses_a_bad_tag_before_touching_git(scopes_workspace, seeded_scopes):
    with pytest.raises(ValueError, match="at most 32 characters"):
        HANDLERS["scope_create"]({"project": "proj-a", "scope": "new", "tags": ["Bad Tag"]})
