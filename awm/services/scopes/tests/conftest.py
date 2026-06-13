"""Shared fixtures for scopes dist tests.

Uses ScopesDAO / init() instead of the legacy awm.db.init_db + state.db.
The key patch is ``awm.persistence.databases.SERVICES_DIR`` — that is the
module-level variable that ``service_db_path()`` reads to locate DB files.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest


@pytest.fixture()
def scopes_workspace(tmp_path, monkeypatch):
    """Set up a temporary workspace with all paths redirected for the scopes service.

    Returns a dict with the same keys as the legacy ``awm_workspace`` fixture
    so ported test code keeps compiling without changes to the callsites.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    awm_dir = workspace / ".awm"
    awm_dir.mkdir()
    services_dir = awm_dir / "services"
    services_dir.mkdir()
    projects_dir = workspace / "projects"
    projects_dir.mkdir()
    skills_dir = workspace / "skills"
    skills_dir.mkdir()
    data_dir = workspace / "data"
    data_dir.mkdir()

    # Redirect config constants used by scopes surface modules.
    monkeypatch.setenv("AWM_WORKSPACE", str(workspace))
    import awm.config as cfg
    monkeypatch.setattr(cfg, "WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(cfg, "AWM_DIR", awm_dir)
    monkeypatch.setattr(cfg, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(cfg, "DATA_DIR", data_dir)
    monkeypatch.setattr(cfg, "SKILLS_DIR", skills_dir)

    # The key patch: databases.service_db_path() uses this module-level var.
    import awm.persistence.databases as pdbs
    monkeypatch.setattr(pdbs, "SERVICES_DIR", services_dir)

    # Patch SKILLS_DIR on the new-location sessions module.
    import awm.scopes.sessions as _sess
    if hasattr(_sess, "SKILLS_DIR"):
        monkeypatch.setattr(_sess, "SKILLS_DIR", skills_dir)

    # Patch PROJECTS_DIR / WORKSPACE_ROOT / DATA_DIR on scopes module.
    import awm.scopes.scopes as _scopes
    for attr, val in [
        ("PROJECTS_DIR", projects_dir),
        ("WORKSPACE_ROOT", workspace),
        ("DATA_DIR", data_dir),
        ("SKILLS_DIR", skills_dir),
    ]:
        if hasattr(_scopes, attr):
            monkeypatch.setattr(_scopes, attr, val)

    # PROJECTS_DIR on projects module.
    import awm.scopes.projects as _projects
    if hasattr(_projects, "PROJECTS_DIR"):
        monkeypatch.setattr(_projects, "PROJECTS_DIR", projects_dir)

    # Reset the DAO's initialized flag so init() runs fresh each test.
    import awm.scopes.dao as scopes_dao
    monkeypatch.setattr(scopes_dao, "_initialized", False)
    scopes_dao.init()

    return {
        "workspace": workspace,
        "awm_dir": awm_dir,
        "services_dir": services_dir,
        "projects_dir": projects_dir,
        "skills_dir": skills_dir,
        "data_dir": data_dir,
    }


@pytest.fixture()
def scopes_dao_conn(scopes_workspace):
    """Return a live connection to the scopes service DB for fixture seeding."""
    from awm.persistence.databases import get_connection
    conn = get_connection("scopes")
    yield conn
    conn.close()


@pytest.fixture()
def seeded_scopes(scopes_workspace, scopes_dao_conn):
    """Seed projects + agents into the scopes DB.

    Three scopes across two projects: one active + two retired.
    Mirrors the legacy ``seeded_scopes`` fixture shape so ported tests
    keep using the same field names without changes.
    """
    from awm.scopes.identity import ensure_project, ensure_agent, now_ms

    workspace = scopes_workspace["workspace"]
    projects_dir = scopes_workspace["projects_dir"]
    conn = scopes_dao_conn
    scope_worktrees = workspace / "test_worktrees"

    scope_data = [
        ("proj-a", "scope-1", "active",    "feat/scope-1",
         str(scope_worktrees / "proj-a" / "scope-1"),
         str(projects_dir / "proj-a" / "scope-1"), 1),
        ("proj-a", "scope-2", "completed", "feat/scope-2",
         str(scope_worktrees / "proj-a" / "scope-2"),
         str(projects_dir / "proj-a" / "scope-2"), 1),
        ("proj-b", "scope-3", "completed", "feat/scope-3",
         str(scope_worktrees / "proj-b" / "scope-3"),
         str(projects_dir / "proj-b" / "scope-3"), 1),
    ]

    for proj_name in ("proj-a", "proj-b"):
        ensure_project(
            proj_name,
            repo_path=str(projects_dir / proj_name / ".bare"),
            conn=conn,
        )

    # "active" → agents status "active"; "completed" → "retired"
    status_map = {"active": "active", "completed": "retired"}
    for project, scope, v_status, branch, worktree, _repo_path, _session in scope_data:
        ensure_agent(
            project, scope,
            branch=branch,
            worktree=worktree,
            agent_cli="claude",
            status=status_map[v_status],
            conn=conn,
        )
    conn.commit()

    # Create per-scope worktree dirs.
    for t in scope_data:
        ws_path = Path(t[4])
        ws_path.mkdir(parents=True, exist_ok=True)
        (ws_path / "AGENTS.md").write_text(f"# {t[0]}/{t[1]}\n")
        (ws_path / "results").mkdir(exist_ok=True)

    # Create project directories + .bare stubs.
    for t in scope_data:
        Path(t[5]).mkdir(parents=True, exist_ok=True)
    for project_name in ("proj-a", "proj-b"):
        (projects_dir / project_name / ".bare").mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).isoformat()
    return [(*t, now, now) for t in scope_data]


@pytest.fixture()
def seeded_sessions(scopes_workspace, seeded_scopes, scopes_dao_conn):
    """Insert session_log rows keyed by (project, scope) inline."""
    from awm.scopes.identity import iso_to_ms
    from datetime import timedelta

    conn = scopes_dao_conn
    base = datetime.now(timezone.utc)
    t1 = base.isoformat()
    t2 = (base + timedelta(seconds=1)).isoformat()
    t3 = (base + timedelta(seconds=2)).isoformat()

    sessions_data = [
        ("proj-a", "scope-1", "", None, t1, "Initial exploration of dataset",
         "agent-1", None, "Initial exploration of dataset"),
        ("proj-a", "scope-1", "", "abc123", t2, "Built feature extraction pipeline",
         "agent-1", '{"decisions": ["Used pandas"]}',
         "Built feature extraction pipeline\n\nDecisions:\n- Used pandas"),
        ("proj-a", "scope-2", "", None, t3, "Reviewed results and documented findings",
         "agent-2", None, "Reviewed results and documented findings"),
    ]
    for s in sessions_data:
        project, scope, file_path, git_commit, logged_at, summary, \
            _agent_label, metadata, content = s
        conn.execute(
            "INSERT INTO session_logs "
            "(project, scope, file_path, git_commit, summary, "
            " metadata, content, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (project, scope, file_path, git_commit, summary,
             metadata, content, iso_to_ms(logged_at)),
        )
    conn.commit()
    return sessions_data
