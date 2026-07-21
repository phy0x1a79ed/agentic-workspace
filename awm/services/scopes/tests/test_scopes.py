"""Tests for awm.scopes.scopes — search, create (mocked), update, delete, heal.

Ported from awm/tests/scopes/test_scopes.py.
- ``from awm.db import get_connection`` → ScopesDAO / scopes_dao_conn
- ``from awm.services import scopes`` → ``from awm.scopes import scopes``
- ``from awm.services.scopes import (...)`` → ``from awm.scopes.scopes import (...)``
- ``from awm.models import ScopeCreateRequest, ScopeUpdateRequest`` → scopes.models
- ``awm_workspace`` → ``scopes_workspace``; ``seeded_scopes`` uses scopes dist conftest
- Legacy _insert_active_scope uses (project, scope) inline columns (no agent_id).
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from awm.scopes.models import ScopeCreateRequest, ScopeUpdateRequest
from awm.scopes import scopes
from awm.scopes.scopes import (
    _CONTEXT_IMPORT_LINE,
    _heal_worktree,
    _is_tracked,
    _strip_context_import,
    heal_scopes,
)


pytestmark = [pytest.mark.scopes, pytest.mark.slow, pytest.mark.subprocess]


class TestSearchScopes:
    def test_empty(self, scopes_workspace):
        result = scopes.search_scopes(status="all")
        assert result.total == 0
        assert result.scopes == []

    def test_all(self, scopes_workspace, seeded_scopes):
        result = scopes.search_scopes(status="all")
        assert result.total == 3

    def test_default_is_active_only(self, scopes_workspace, seeded_scopes):
        result = scopes.search_scopes()
        assert result.total == 1
        assert result.scopes[0].status == "active"

    def test_by_status(self, scopes_workspace, seeded_scopes):
        result = scopes.search_scopes(status="active")
        assert result.total == 1
        assert result.scopes[0].status == "active"

    def test_by_project(self, scopes_workspace, seeded_scopes):
        result = scopes.search_scopes(status="all", project="proj-a")
        assert result.total == 2

    def test_combined_filters(self, scopes_workspace, seeded_scopes):
        result = scopes.search_scopes(status="completed", project="proj-b")
        assert result.total == 1
        assert result.scopes[0].scope == "scope-3"

    def test_status_all(self, scopes_workspace, seeded_scopes):
        result = scopes.search_scopes(status="all")
        assert result.total == 3

    def test_includes_repo_path(self, scopes_workspace, seeded_scopes):
        result = scopes.search_scopes(status="active")
        # v1: repo_path is populated (either worktree or bare repo path).
        assert result.scopes[0].repo_path is not None
        assert result.scopes[0].repo_path != ""


def _seed_bare_project(projects_dir: Path, project: str) -> Path:
    """Create projects/<project>/.bare with one commit on `main` (real git)."""
    seed = projects_dir / f"{project}-seed"
    seed.mkdir(parents=True)
    subprocess.run(["git", "-C", str(seed), "init", "-q", "-b", "main"], check=True)
    (seed / "README").write_text("x\n")
    subprocess.run(["git", "-C", str(seed), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(seed), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-q", "-m", "init"], check=True)
    bare = projects_dir / project / ".bare"
    bare.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--bare", "-q", str(seed), str(bare)],
                   check=True, capture_output=True)
    return bare


class TestCreateScope:
    def test_create_missing_project(self, scopes_workspace):
        req = ScopeCreateRequest(project="nope", scope="s1")
        with pytest.raises(FileNotFoundError, match="not found"):
            scopes.create_scope(req)

    def test_default_branch_is_feat_prefixed(self, scopes_workspace):
        projects_dir = scopes_workspace["projects_dir"]
        _seed_bare_project(projects_dir, "bp")
        scopes.create_scope(ScopeCreateRequest(project="bp", scope="work"))
        worktree = projects_dir / "bp" / "work"
        branch = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert branch == "feat/work"

    def test_branch_name_override_names_plain_branch(self, scopes_workspace):
        """branch_name lets a scope live on a plain branch like `release`."""
        projects_dir = scopes_workspace["projects_dir"]
        _seed_bare_project(projects_dir, "bp2")
        scopes.create_scope(
            ScopeCreateRequest(project="bp2", scope="release", branch_name="release")
        )
        worktree = projects_dir / "bp2" / "release"
        branch = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert branch == "release"
        assert (worktree / ".awm" / "context.md").exists()


class TestUpdateScope:
    def test_complete_scope(self, scopes_workspace, seeded_scopes, monkeypatch):
        monkeypatch.setattr(
            "awm.scopes.git_utils.run_git",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, returncode=1),
        )
        bare_dir = scopes_workspace["projects_dir"] / "proj-a" / ".bare"
        bare_dir.mkdir(parents=True, exist_ok=True)

        req = ScopeUpdateRequest(action="complete")
        result = scopes.update_scope("proj-a", "scope-1", req)
        assert result.status == "completed"

        db_result = scopes.search_scopes(status="completed")
        completed = [s.scope for s in db_result.scopes]
        assert "scope-1" in completed

    def test_update_missing_project(self, scopes_workspace):
        req = ScopeUpdateRequest(action="complete")
        with pytest.raises(FileNotFoundError):
            scopes.update_scope("nope", "nope", req)

    def test_invalid_action_rejected(self, scopes_workspace, seeded_scopes):
        with pytest.raises(Exception):
            ScopeUpdateRequest(action="pause")


class TestDeleteScope:
    def test_delete_scope(self, scopes_workspace, seeded_scopes, monkeypatch):
        monkeypatch.setattr(
            "awm.scopes.git_utils.run_git",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, returncode=0),
        )
        bare_dir = scopes_workspace["projects_dir"] / "proj-a" / ".bare"
        bare_dir.mkdir(parents=True, exist_ok=True)

        result = scopes.delete_scope("proj-a", "scope-1")
        assert result.status == "deleted"

        # v1: the agents table uses "retired" for deleted scopes; there is no
        # separate "deleted" status in the DB. search_scopes(status="deleted")
        # always returns empty. Verify the scope is no longer active instead.
        active_result = scopes.search_scopes(status="active", project="proj-a")
        active_scopes = [s.scope for s in active_result.scopes]
        assert "scope-1" not in active_scopes

    def test_delete_missing_scope(self, scopes_workspace):
        bare_dir = scopes_workspace["projects_dir"] / "nope" / ".bare"
        bare_dir.mkdir(parents=True, exist_ok=True)
        with pytest.raises(FileNotFoundError):
            scopes.delete_scope("nope", "nope")


# ---------------------------------------------------------------------------
# Tier-3 cleanup helpers (pure logic, no DB required)
# ---------------------------------------------------------------------------


def _git_init_tracked_agents(worktree: Path, body: str = "# tracked\n") -> None:
    """Init a real git repo with AGENTS.md committed at HEAD."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True, env=env)
    (worktree / "AGENTS.md").write_text(body)
    subprocess.run(["git", "add", "AGENTS.md"], cwd=worktree, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"],
                   cwd=worktree, check=True, env=env)


def _git_init_no_track(worktree: Path) -> None:
    """Init a real git repo with nothing committed."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True, env=env)


class TestStripContextImport:
    def test_strips_trailing_line(self):
        text = "# title\n\nbody\n\n@.awm/context.md\n"
        new, changed = _strip_context_import(text)
        assert changed is True
        assert new == "# title\n\nbody\n"

    def test_no_op_when_absent(self):
        text = "# title\n\nbody\n"
        new, changed = _strip_context_import(text)
        assert changed is False
        assert new == text

    def test_ignores_middle_occurrence(self):
        text = "# title\n@.awm/context.md\nbody\n"
        new, changed = _strip_context_import(text)
        assert changed is False
        assert new == text

    def test_no_orphan_blank_line(self):
        text = "# title\nbody\n@.awm/context.md\n"
        new, changed = _strip_context_import(text)
        assert changed is True
        assert new == "# title\nbody\n"


class TestIsTracked:
    def test_true_for_tracked(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        _git_init_tracked_agents(wt)
        assert _is_tracked(wt, "AGENTS.md") is True

    def test_false_for_untracked(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        _git_init_no_track(wt)
        (wt / "AGENTS.md").write_text("# floating\n")
        assert _is_tracked(wt, "AGENTS.md") is False

    def test_false_for_missing(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        _git_init_no_track(wt)
        assert _is_tracked(wt, "AGENTS.md") is False


class TestHealWorktree:
    @pytest.fixture(autouse=True)
    def _isolate(self, scopes_workspace):
        """Heal reaches outside the worktree now — it also reconciles
        ``.awm/data`` against the project's canonical data dir. Without the
        workspace fixture these tests would create ``proj-a/`` in the REAL
        ``data/``."""
        return scopes_workspace

    def test_strips_leaked_import_from_tracked_agents(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        _git_init_tracked_agents(wt, body="# project AGENTS\nbody\n")
        (wt / "AGENTS.md").write_text(
            "# project AGENTS\nbody\n\n@.awm/context.md\n"
        )

        actions = _heal_worktree(wt, project="proj-a", scope="s1", dry_run=False)

        assert actions["import_line"] == "stripped"
        assert (wt / "AGENTS.md").read_text() == "# project AGENTS\nbody\n"
        assert actions["agents_md"] is None

    def test_deletes_untracked_agents_md(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        _git_init_no_track(wt)
        (wt / "AGENTS.md").write_text("# scope-local chrome\n")

        actions = _heal_worktree(wt, project="proj-a", scope="s1", dry_run=False)

        assert actions["agents_md"] == "deleted:untracked"
        assert not (wt / "AGENTS.md").exists()

    def test_deletes_claude_symlink_to_agents(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        _git_init_tracked_agents(wt)
        (wt / "CLAUDE.md").symlink_to("AGENTS.md")

        actions = _heal_worktree(wt, project="proj-a", scope="s1", dry_run=False)

        assert actions["claude_md"] == "deleted:symlink"
        assert not (wt / "CLAUDE.md").exists()
        assert not (wt / "CLAUDE.md").is_symlink()

    def test_keeps_tracked_claude_md(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(["git", "init", "-q"], cwd=wt, check=True, env=env)
        (wt / "CLAUDE.md").write_text("# real tracked claude\n")
        subprocess.run(["git", "add", "CLAUDE.md"], cwd=wt, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "init"],
                       cwd=wt, check=True, env=env)

        actions = _heal_worktree(wt, project="proj-a", scope="s1", dry_run=False)

        assert actions["claude_md"] is None
        assert (wt / "CLAUDE.md").read_text() == "# real tracked claude\n"

    def test_backfills_missing_context_md(self, tmp_path, scopes_workspace):
        wt = tmp_path / "wt"
        wt.mkdir()
        _git_init_tracked_agents(wt)

        actions = _heal_worktree(wt, project="proj-a", scope="s1", dry_run=False)

        assert actions["context_md"] == "created"
        assert (wt / ".awm" / "context.md").exists()
        body = (wt / ".awm" / "context.md").read_text()
        assert "proj-a/s1" in body

    def test_refreshes_stale_opencode_config(self, tmp_path, scopes_workspace):
        wt = tmp_path / "wt"
        wt.mkdir()
        _git_init_tracked_agents(wt)
        (scopes_workspace["workspace"] / "WORKSPACE.md").write_text("# orient\n")
        awm = wt / ".awm"
        awm.mkdir()
        (awm / "mcp-opencode.json").write_text(
            '{"$schema":"x","mcp":{},"instructions":["legacy.md"]}\n'
        )

        actions = _heal_worktree(wt, project="proj-a", scope="s1", dry_run=False)

        assert actions["opencode_config"] == "rewritten"
        import json
        cfg = json.loads((awm / "mcp-opencode.json").read_text())
        assert cfg["instructions"] == [
            str(scopes_workspace["workspace"] / "WORKSPACE.md"),
            ".awm/context.md",
        ]

    def test_creates_missing_opencode_config(self, tmp_path, scopes_workspace):
        wt = tmp_path / "wt"
        wt.mkdir()
        _git_init_tracked_agents(wt)
        (scopes_workspace["workspace"] / "WORKSPACE.md").write_text("# orient\n")

        actions = _heal_worktree(wt, project="proj-a", scope="s1", dry_run=False)

        assert actions["opencode_config"] == "created"
        assert (wt / ".awm" / "mcp-opencode.json").is_file()

    def test_opencode_config_dry_run_reports_would_create(self, tmp_path, scopes_workspace):
        wt = tmp_path / "wt"
        wt.mkdir()
        _git_init_tracked_agents(wt)

        actions = _heal_worktree(wt, project="proj-a", scope="s1", dry_run=True)

        assert actions["opencode_config"] == "would-create"
        assert not (wt / ".awm" / "mcp-opencode.json").exists()

    def test_leaves_existing_context_md_alone(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        _git_init_tracked_agents(wt)
        (wt / ".awm").mkdir()
        original = "# my custom context\n"
        (wt / ".awm" / "context.md").write_text(original)

        actions = _heal_worktree(wt, project="proj-a", scope="s1", dry_run=False)

        assert actions["context_md"] is None
        assert (wt / ".awm" / "context.md").read_text() == original

    def test_dry_run_does_not_mutate(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        _git_init_tracked_agents(wt, body="# project\n")
        (wt / "AGENTS.md").write_text("# project\n\n@.awm/context.md\n")
        (wt / "CLAUDE.md").symlink_to("AGENTS.md")

        before_agents = (wt / "AGENTS.md").read_text()

        actions = _heal_worktree(wt, project="proj-a", scope="s1", dry_run=True)

        assert actions["import_line"] == "stripped"
        assert actions["claude_md"] == "deleted:symlink"
        assert actions["context_md"] == "created"
        assert (wt / "AGENTS.md").read_text() == before_agents
        assert (wt / "CLAUDE.md").is_symlink()
        assert not (wt / ".awm" / "context.md").exists() if (wt / ".awm").exists() else True

    def test_idempotent(self, tmp_path, scopes_workspace):
        wt = tmp_path / "wt"
        wt.mkdir()
        _git_init_tracked_agents(wt)
        (wt / "CLAUDE.md").symlink_to("AGENTS.md")

        _heal_worktree(wt, project="proj-a", scope="s1", dry_run=False)
        agents_after = (wt / "AGENTS.md").read_text()
        ctx_after = (wt / ".awm" / "context.md").read_text()

        second = _heal_worktree(wt, project="proj-a", scope="s1", dry_run=False)

        assert second == {
            "import_line": None, "agents_md": None,
            "claude_md": None, "context_md": None,
            "opencode_config": None, "data": None,
        }
        assert (wt / "AGENTS.md").read_text() == agents_after
        assert (wt / ".awm" / "context.md").read_text() == ctx_after


# ---------------------------------------------------------------------------
# heal_scopes — DB-driven cleanup pass
# ---------------------------------------------------------------------------


def _insert_active_scope(conn, *, project: str, scope: str, worktree: Path) -> None:
    """Insert an active agents row via identity.ensure_agent."""
    from awm.scopes.identity import ensure_project, ensure_agent
    ensure_project(project, repo_path=str(worktree.parent / ".bare"), conn=conn)
    ensure_agent(
        project, scope,
        branch=f"feat/{scope}",
        worktree=str(worktree),
        agent_cli="claude",
        status="active",
        conn=conn,
    )
    conn.commit()


class TestHealScopes:
    def test_skips_inactive_scopes(self, scopes_workspace, seeded_scopes):
        report = heal_scopes()
        assert len(report) == 1
        assert report[0]["scope"] == "scope-1"
        assert report[0]["ok"] is True

    def test_reports_missing_worktree(self, scopes_workspace, seeded_scopes,
                                      scopes_dao_conn):
        bogus = "/tmp/awm-test-does-not-exist-xyz"
        scopes_dao_conn.execute(
            "UPDATE agents SET worktree=? WHERE scope='scope-1'", (bogus,),
        )
        scopes_dao_conn.commit()
        report = heal_scopes()
        assert len(report) == 1
        assert report[0]["ok"] is False
        assert report[0]["error"].startswith("worktree missing")
        assert bogus in report[0]["error"]

    def test_empty_worktree_never_resolves_to_the_cwd(self, scopes_workspace,
                                                      seeded_scopes,
                                                      scopes_dao_conn):
        """An empty worktree column must never be healed against the cwd.

        ``Path("")`` is ``Path(".")``, which *exists*, so an existence check
        alone waves the row through and heal then operates on whatever
        directory the process happens to be standing in. In production this
        repointed a live scope's ``.awm/data`` symlink and left a stray clone
        inside the project's canonical data directory. It must fall back to the
        scope's conventional location instead.
        """
        from awm.config import PROJECTS_DIR
        scopes_dao_conn.execute("UPDATE agents SET worktree='' WHERE scope='scope-1'")
        scopes_dao_conn.commit()
        before = sorted(p.name for p in Path.cwd().iterdir())

        report = heal_scopes()

        assert len(report) == 1
        resolved = Path(report[0]["worktree"])
        assert resolved.is_absolute()
        assert resolved == PROJECTS_DIR / "proj-a" / "scope-1"
        assert resolved != Path.cwd()
        assert sorted(p.name for p in Path.cwd().iterdir()) == before

    def test_anchors_relative_worktree_on_the_workspace(self, scopes_workspace,
                                                        seeded_scopes,
                                                        scopes_dao_conn):
        """A workspace-relative row resolves against the workspace, not the cwd."""
        from awm.config import PROJECTS_DIR
        wt = PROJECTS_DIR / "proj-a" / "rel-scope"
        wt.mkdir(parents=True)
        rel = str(wt.relative_to(PROJECTS_DIR.parent))
        assert not Path(rel).is_absolute()
        scopes_dao_conn.execute(
            "UPDATE agents SET worktree=? WHERE scope='scope-1'", (rel,))
        scopes_dao_conn.commit()

        report = heal_scopes()

        assert len(report) == 1
        assert report[0]["ok"] is True, report[0]

    def test_filters_by_project(self, scopes_workspace, seeded_scopes,
                                 scopes_dao_conn, tmp_path):
        proj_b_wt = tmp_path / "extra" / "proj-b" / "active-2"
        proj_b_wt.mkdir(parents=True)
        _insert_active_scope(scopes_dao_conn, project="proj-b", scope="active-2",
                             worktree=proj_b_wt)

        report = heal_scopes(project="proj-a")
        assert {r["project"] for r in report} == {"proj-a"}

        report_all = heal_scopes()
        assert {r["project"] for r in report_all} == {"proj-a", "proj-b"}

    def test_cleans_up_active_worktree(self, scopes_workspace, seeded_scopes):
        from awm.scopes import scopes as scopes_mod
        active = scopes_mod.search_scopes(status="active").scopes[0]
        wt = Path(active.worktree)
        _git_init_no_track(wt)
        (wt / "CLAUDE.md").symlink_to("AGENTS.md")

        report = heal_scopes()

        assert report[0]["ok"] is True
        actions = report[0]["actions"]
        assert actions["agents_md"] == "deleted:untracked"
        assert actions["claude_md"] == "deleted:symlink"
        assert actions["context_md"] == "created"
        assert not (wt / "AGENTS.md").exists()
        assert not (wt / "CLAUDE.md").exists()
        assert (wt / ".awm" / "context.md").exists()

    def test_dry_run_reports_without_mutating(self, scopes_workspace, seeded_scopes):
        from awm.scopes import scopes as scopes_mod
        active = scopes_mod.search_scopes(status="active").scopes[0]
        wt = Path(active.worktree)
        _git_init_no_track(wt)
        (wt / "CLAUDE.md").symlink_to("AGENTS.md")

        report = heal_scopes(dry_run=True)

        assert report[0]["dry_run"] is True
        actions = report[0]["actions"]
        assert actions["agents_md"] == "deleted:untracked"
        assert (wt / "AGENTS.md").exists()
        assert (wt / "CLAUDE.md").is_symlink()

    def test_idempotent_across_runs(self, scopes_workspace, seeded_scopes):
        from awm.scopes import scopes as scopes_mod
        active = scopes_mod.search_scopes(status="active").scopes[0]
        wt = Path(active.worktree)
        _git_init_no_track(wt)

        heal_scopes()
        second = heal_scopes()
        assert second[0]["actions"] == {
            "import_line": None, "agents_md": None,
            "claude_md": None, "context_md": None,
            "opencode_config": None, "data": None,
        }


class TestArtifactsPointer:
    """`.awm/artifacts.md` is a bounded discoverability pointer, not an index.

    It is read on every session's startup ritual, so it must teach how to *find*
    reusable outputs and stay a fixed size — never inline the (unbounded) list.
    """

    def test_pointer_teaches_discovery_not_a_list(self):
        from awm.scopes.scopes import _generate_artifacts_md

        md = _generate_artifacts_md("awm", "dev-misc")

        # Discoverability: what/when/how to find and reuse sibling outputs.
        assert "artifact_search project=awm" in md
        assert "artifact_register project=awm scope=dev-misc" in md
        assert "pointer, not a list" in md
        assert "Reuse beats recompute" in md
        # Not the old content-free stub.
        assert "Artifact index is managed by the artifacts service" not in md

    def test_pointer_is_bounded_and_lists_nothing(self):
        from awm.scopes.scopes import _generate_artifacts_md

        # Content depends only on project/scope names, never on how many
        # artifacts exist — so the file cannot grow into a force-read index.
        md = _generate_artifacts_md("awm", "dev-misc")
        assert len(md) < 1500
        # No per-artifact rows (a real index would enumerate them).
        assert "\n- " not in md.split("**How to look**", 1)[0]
