"""The project verbs folded into scope: ``new_project``, the project listing, the main checkout."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awm.scopes import scopes
from awm.scopes.models import ScopeCreateRequest

pytestmark = [pytest.mark.scopes, pytest.mark.slow, pytest.mark.subprocess]


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)


@pytest.fixture()
def seed_repo(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "README").write_text("hello\n")
    _git(seed, "add", "README")
    _git(seed, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    return seed


@pytest.fixture()
def hand_made_project(scopes_workspace, seed_repo):
    """A bare repo with a main checkout and no agents row, as set up by hand."""
    projects_dir = scopes_workspace["projects_dir"]
    bare = projects_dir / "hand" / ".bare"
    bare.parent.mkdir()
    subprocess.run(["git", "clone", "--bare", "-q", str(seed_repo), str(bare)],
                   check=True, capture_output=True)
    _git(bare, "worktree", "add", "../main", "main")
    return projects_dir / "hand" / "main"


class TestNewProject:
    def test_an_unknown_project_is_refused_and_the_error_names_the_flag(self, scopes_workspace):
        with pytest.raises(FileNotFoundError, match="new_project=true"):
            scopes.create_scope(ScopeCreateRequest(project="nope", scope="s1"))
        assert not (scopes_workspace["projects_dir"] / "nope").exists()

    def test_new_project_creates_the_project_and_the_scope(self, scopes_workspace, seed_repo):
        projects_dir = scopes_workspace["projects_dir"]
        res = scopes.create_scope(ScopeCreateRequest(
            project="fresh", scope="work", new_project=True, clone_url=str(seed_repo)))
        assert res.scope == "work"
        assert (projects_dir / "fresh" / ".bare" / "HEAD").exists()
        assert (projects_dir / "fresh" / "main" / ".awm" / "context.md").exists()
        assert (projects_dir / "fresh" / "work" / ".awm" / "context.md").exists()

    def test_new_project_on_a_known_project_changes_nothing(self, scopes_workspace, seed_repo):
        scopes.create_scope(ScopeCreateRequest(
            project="fresh", scope="one", new_project=True, clone_url=str(seed_repo)))
        res = scopes.create_scope(ScopeCreateRequest(project="fresh", scope="two", new_project=True))
        assert res.scope == "two"

    def test_new_project_without_a_scope_creates_the_project_alone(self, scopes_workspace, seed_repo):
        projects_dir = scopes_workspace["projects_dir"]
        res = scopes.create_scope(ScopeCreateRequest(
            project="solo", new_project=True, clone_url=str(seed_repo)))
        assert (res.project, res.scope, res.status) == ("solo", "main", "active")
        assert (projects_dir / "solo" / "main" / ".awm" / "context.md").exists()
        assert sorted(p.name for p in (projects_dir / "solo").iterdir() if p.name != ".bare") == ["main"]

    def test_create_tags_the_scope_or_the_project_made_alone(self, scopes_workspace, seed_repo):
        from awm.scopes import tags
        scopes.create_scope(ScopeCreateRequest(
            project="solo", new_project=True, clone_url=str(seed_repo), tags=["tony"]))
        scopes.create_scope(ScopeCreateRequest(project="solo", scope="lab/ana", tags=["lab", "ana"]))
        assert tags.load("solo") == {("solo", ""): ["tony"], ("solo", "lab/ana"): ["ana", "lab"]}

    def test_a_scope_is_required_unless_the_project_is_being_created(self, scopes_workspace, seed_repo):
        with pytest.raises(ValueError, match="new_project=true"):
            scopes.create_scope(ScopeCreateRequest(project="solo"))
        scopes.create_scope(ScopeCreateRequest(project="solo", new_project=True, clone_url=str(seed_repo)))
        with pytest.raises(FileExistsError, match="already exists"):
            scopes.create_scope(ScopeCreateRequest(project="solo", new_project=True))

    def test_the_handler_forwards_the_flag(self, scopes_workspace, monkeypatch):
        from awm.scopes.operations.scopes import SCOPE_HANDLERS
        seen = {}

        class _Result:
            def model_dump(self):
                return {}

        monkeypatch.setattr(scopes, "create_scope", lambda req: seen.setdefault("req", req) and _Result())
        SCOPE_HANDLERS["scope_create"]({"project": "p", "scope": "s", "new_project": True,
                                        "clone_url": "u"})
        assert seen["req"].new_project is True and seen["req"].clone_url == "u"


class TestEnsureScopeRpc:
    def test_an_unknown_project_is_refused_and_the_error_names_the_flag(self, scopes_workspace):
        from awm.scopes.hub_adapter import _handle_ensure_scope
        with pytest.raises(KeyError, match="new_project=true"):
            _handle_ensure_scope({"project": "ghost", "scope": "s"})

    def test_new_project_registers_the_project_then_the_scope(self, scopes_workspace):
        from awm.scopes.hub_adapter import _handle_ensure_scope
        from awm.scopes.identity import agent_id_for_scope, project_id_for_name
        out = _handle_ensure_scope({"project": "ghost", "scope": "s", "new_project": True})
        assert out == {"project": "ghost", "scope": "s"}
        assert project_id_for_name("ghost") and agent_id_for_scope("ghost", "s")

    def test_the_old_calling_order_still_works(self, scopes_workspace):
        from awm.scopes.hub_adapter import _handle_ensure_project, _handle_ensure_scope
        assert _handle_ensure_project({"project": "old"}) == {"project": "old"}
        assert _handle_ensure_scope({"project": "old", "scope": "s"}) == {"project": "old", "scope": "s"}


class TestMainCheckout:
    def test_a_hand_made_main_checkout_is_a_scope_of_its_project(self, hand_made_project):
        found = scopes.search_scopes(project="hand")
        assert [(s.scope, s.status, s.branch) for s in found.scopes] == [("main", "active", "main")]
        assert found.scopes[0].worktree == str(hand_made_project)

    def test_a_project_scoped_search_does_not_list_projects(self, hand_made_project):
        from awm.scopes.operations.scopes import SCOPE_HANDLERS
        assert "projects" not in SCOPE_HANDLERS["scope_search"]({"project": "hand"})

    def test_a_completed_filter_leaves_the_main_checkout_out(self, hand_made_project):
        assert scopes.search_scopes(project="hand", status="completed").scopes == []

    def test_a_query_filters_the_main_checkout_by_name(self, hand_made_project):
        assert scopes.search_scopes(project="hand", query="zzz").scopes == []
        assert [s.scope for s in scopes.search_scopes(project="hand", query="mai").scopes] == ["main"]

    def test_a_scaffolded_main_checkout_is_listed_once(self, scopes_workspace, seed_repo):
        scopes.create_scope(ScopeCreateRequest(
            project="fresh", scope="work", new_project=True, clone_url=str(seed_repo)))
        found = scopes.search_scopes(project="fresh")
        assert sorted(s.scope for s in found.scopes) == ["main", "work"]


class TestProjectListing:
    def test_a_scope_limit_never_shortens_the_project_list(self, seeded_scopes, hand_made_project):
        every = {p.name for p in scopes.search_scopes(status="all").projects}
        limited = scopes.search_scopes(status="all", limit=1)
        assert len(limited.scopes) == 1
        assert {p.name for p in limited.projects} == every and len(every) > 1

    def test_no_project_lists_every_project_with_counts_and_its_main_checkout(
            self, seeded_scopes, hand_made_project):
        found = scopes.search_scopes(status="all")
        counts = {p.name: p.scope_counts for p in found.projects}
        assert set(counts) == {"proj-a", "proj-b", "hand"}
        assert (counts["proj-a"].active, counts["proj-a"].completed) == (1, 1)
        assert (counts["proj-b"].active, counts["proj-b"].completed) == (0, 1)
        assert counts["hand"].active == 1

    def test_a_query_narrows_the_project_list_by_name(self, seeded_scopes, hand_made_project):
        found = scopes.search_scopes(query="proj-b", status="all")
        assert [p.name for p in found.projects][0] == "proj-b"

    def test_the_handler_reply_carries_the_projects(self, seeded_scopes):
        from awm.scopes.operations.scopes import SCOPE_HANDLERS
        reply = SCOPE_HANDLERS["scope_search"]({"status": "all"})
        assert {p["name"] for p in reply["projects"]} == {"proj-a", "proj-b"}
        assert reply["projects"][0]["scope_counts"].keys() == {"active", "completed", "deleted"}
