"""Tests for the scope agent (spawn_scoped / list_scopes).

The scope agent is the second, scope-based implementation: a resolve-or-provision
preamble around the shared ``fleet_spawn.spawn_terminal`` leaf. These cover the
provisioning fork (create only when the worktree is absent), the derived
worktree path, and the list_scopes passthrough — with the tmux launch and the
scopes RPC both stubbed (the real launch rides the live-hub harness)."""

from __future__ import annotations

import asyncio

import pytest

pytestmark = [pytest.mark.agent, pytest.mark.smoke]

from awm.agents import scope_spawn


@pytest.fixture
def projects_dir(tmp_path, monkeypatch):
    pdir = tmp_path / "projects"
    pdir.mkdir()
    monkeypatch.setattr("awm.config.PROJECTS_DIR", pdir)
    # scope_spawn imports the config module (not the symbol), so patching the
    # module attribute is what its worktree_path reads.
    import awm.config as cfg
    monkeypatch.setattr(cfg, "PROJECTS_DIR", pdir)
    return pdir


@pytest.fixture
def stub_spawn(monkeypatch):
    """Record spawn_terminal calls; return a deterministic result."""
    calls = []

    def _fake_spawn(*, cwd, harness="claude", model=None, effort=None,
                    permission="default", hub_url=None):
        calls.append({"cwd": cwd, "harness": harness, "model": model,
                      "effort": effort, "permission": permission})
        return {"tmux_session": "fleet-x-1", "cwd": cwd, "harness": harness,
                "model": model, "effort": effort, "command": "claude"}

    monkeypatch.setattr(scope_spawn.fleet_spawn, "spawn_terminal", _fake_spawn)
    return calls


@pytest.fixture
def stub_gw(monkeypatch):
    """Record + fake gatewayclient.call (scope_create / scope_search)."""
    calls = []

    async def _fake_call(service, fn, args=None, *, timeout=None, **kw):
        calls.append((service, fn, args or {}))
        if fn == "scope_create":
            # Simulate provisioning: lay the worktree dir down on disk.
            import awm.config as cfg
            (cfg.PROJECTS_DIR / args["project"] / args["scope"]).mkdir(
                parents=True, exist_ok=True)
            return {"project": args["project"], "scope": args["scope"],
                    "status": "active", "message": "created"}
        if fn == "scope_search":
            return {"scopes": [
                {"project": "awm", "scope": "svc-x", "status": "active",
                 "branch": "feat/svc-x", "worktree": "/w/awm/svc-x"},
                {"project": "awm", "scope": "svc-y", "status": "active",
                 "branch": "feat/svc-y"},  # no worktree → derived
            ]}
        return {"ok": True}

    import awm.gatewayclient as gw
    monkeypatch.setattr(gw, "call", _fake_call)
    return calls


class TestWorktreePath:
    def test_derives_projects_dir_layout(self, projects_dir):
        assert scope_spawn.worktree_path("awm", "svc-agents") == str(
            projects_dir / "awm" / "svc-agents")


class TestSpawnScoped:
    def test_requires_project_and_scope(self, projects_dir):
        with pytest.raises(ValueError):
            asyncio.run(scope_spawn.spawn_scoped(project="", scope="x"))
        with pytest.raises(ValueError):
            asyncio.run(scope_spawn.spawn_scoped(project="awm", scope=""))

    def test_provisions_when_worktree_absent(self, projects_dir, stub_spawn, stub_gw):
        out = asyncio.run(scope_spawn.spawn_scoped(
            project="awm", scope="new-scope", model="claude-sonnet-5"))
        # scope_create was called (worktree didn't exist) …
        assert ("scopes", "scope_create", {"project": "awm", "scope": "new-scope"}) \
            in stub_gw
        # … then spawn_terminal launched IN the derived worktree.
        assert stub_spawn[0]["cwd"] == str(projects_dir / "awm" / "new-scope")
        assert out["project"] == "awm" and out["scope"] == "new-scope"
        assert out["tmux_session"] == "fleet-x-1"

    def test_skips_create_when_worktree_present(self, projects_dir, stub_spawn, stub_gw):
        (projects_dir / "awm" / "exists").mkdir(parents=True)
        asyncio.run(scope_spawn.spawn_scoped(
            project="awm", scope="exists", model="haiku"))
        # No scope_create when the worktree is already on disk (not idempotent).
        assert not [c for c in stub_gw if c[1] == "scope_create"]
        assert stub_spawn[0]["cwd"] == str(projects_dir / "awm" / "exists")

    def test_passes_context_to_create(self, projects_dir, stub_spawn, stub_gw):
        asyncio.run(scope_spawn.spawn_scoped(
            project="awm", scope="ctx-scope", model="haiku",
            context="do the thing"))
        create = [c for c in stub_gw if c[1] == "scope_create"][0]
        assert create[2]["context"] == "do the thing"


class TestListScopes:
    def test_maps_rows_and_derives_missing_worktree(self, projects_dir, stub_gw):
        out = asyncio.run(scope_spawn.list_scopes(project="awm"))
        scopes = out["scopes"]
        assert scopes[0]["worktree"] == "/w/awm/svc-x"      # from the service
        assert scopes[1]["worktree"] == str(               # derived
            projects_dir / "awm" / "svc-y")
        assert {s["scope"] for s in scopes} == {"svc-x", "svc-y"}
