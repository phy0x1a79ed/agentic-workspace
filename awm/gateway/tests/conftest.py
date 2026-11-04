"""Shared fixtures for the awm-gateway dist tests.

The gateway is de-DB'd — it owns no tables (per-service DBs live in each
feature dist). So this conftest does NOT init any database; it only
redirects the ``awm.config`` path constants into a tmp workspace so the
service journal, access log, etc. land under ``tmp_path`` instead of the
real ``.awm/``.

Provides an ``awm_workspace`` fixture with the same dict shape the legacy
monolith fixture returned, so ported hub/supervisor tests keep compiling
against ``awm_workspace["awm_dir"]`` / ``["workspace"]`` without edits.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def awm_workspace(tmp_path, monkeypatch):
    """Redirect gateway path constants into a tmp workspace (no DB)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    awm_dir = workspace / ".awm"
    awm_dir.mkdir()
    projects_dir = workspace / "projects"
    projects_dir.mkdir()
    data_dir = workspace / "data"
    data_dir.mkdir()

    # Isolate filesystem discovery: without these, ``discovery.services_root()``
    # / ``pages_root()`` resolve to the REAL running worktree, so any
    # TestClient-driven lifespan bootstraps the live services (spawning real
    # subprocesses) and auto-registers the live pages — polluting "empty
    # registry" assertions and slowing every hub test. Point both at empty dirs;
    # tests that exercise discovery set their own (a later setenv wins).
    empty_services = tmp_path / "empty_services"
    empty_services.mkdir()
    empty_pages = tmp_path / "empty_pages"
    empty_pages.mkdir()
    monkeypatch.setenv("AWM_SERVICES_DIR", str(empty_services))
    monkeypatch.setenv("AWM_PAGES_DIR", str(empty_pages))

    import awm.config as cfg
    monkeypatch.setattr(cfg, "WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(cfg, "AWM_DIR", awm_dir)
    monkeypatch.setattr(cfg, "PID_FILE", awm_dir / "awm.pid")
    monkeypatch.setattr(cfg, "LOG_FILE", awm_dir / "awm.log")
    monkeypatch.setattr(cfg, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(cfg, "DATA_DIR", data_dir)
    # SERVICES_DIR is a module constant frozen at import (= AWM_DIR / "services"),
    # so patching AWM_DIR alone leaves it pointing at the REAL workspace. The
    # service-enable state (`discovery.is_enabled` → SERVICES_DIR/enabled.json)
    # lives under it; without this patch, supervisor/discovery tests read AND
    # write the live workspace's enabled.json — a service disabled there
    # silently skips a respawn assertion, and a test's set_enabled pollutes
    # prod. Redirect it into tmp_path too.
    if hasattr(cfg, "SERVICES_DIR"):
        monkeypatch.setattr(cfg, "SERVICES_DIR", awm_dir / "services")
    if hasattr(cfg, "ACCESS_LOG"):
        monkeypatch.setattr(cfg, "ACCESS_LOG", awm_dir / "access.log")

    # The service supervisor reads AWM_DIR from awm.config at call time
    # via its own import; patch there too if it took a module-level copy.
    import awm.gateway.hub.supervisor as sup
    for attr in ("AWM_DIR",):
        if hasattr(sup, attr):
            monkeypatch.setattr(sup, attr, awm_dir)

    # Server module may hold copies of the lifecycle paths.
    try:
        import awm.gateway.server as srv
        for attr, val in [
            ("WORKSPACE_ROOT", workspace),
            ("PID_FILE", awm_dir / "awm.pid"),
            ("LOG_FILE", awm_dir / "awm.log"),
            ("IDLE_SHUTDOWN_SECONDS", 0),
        ]:
            if hasattr(srv, attr):
                monkeypatch.setattr(srv, attr, val)
    except Exception:
        pass

    return {
        "workspace": workspace,
        "awm_dir": awm_dir,
        "projects_dir": projects_dir,
        "data_dir": data_dir,
    }


#: The peers the hand-built snapshots in the surface tests name. The fleet edge
#: list every test runs under gives this node an edge to each of them and each
#: of them an edge back.
EDGE_PEERS = ("altair", "mira", "cosmos", "capella", "fir")


@pytest.fixture(autouse=True)
def fleet_edges(tmp_path_factory, monkeypatch):
    """A fleet edge list (``AWM_EDGES_FILE``) joining this node and
    :data:`EDGE_PEERS` both ways. A node it does not name has no edge."""
    import awm.config as config
    from awm.config import edges

    here = edges.label(config.node_name())
    names = sorted({here, *EDGE_PEERS})
    lines = ["nodes:"] + [f"  {n}: {{role: homebase}}" for n in names] + ["edges:"]
    lines.append(f"  {here}: {{{', '.join(f'{p}: lan' for p in EDGE_PEERS if p != here)}}}")
    lines += [f"  {p}: {{{here}: lan}}" for p in EDGE_PEERS if p != here]
    path = tmp_path_factory.mktemp("fleet") / "edges.yml"
    path.write_text("\n".join(lines) + "\n")
    monkeypatch.setenv(edges.EDGES_FILE_ENV, str(path))
    return path


@pytest.fixture(autouse=True)
def _isolated_usage_tally(tmp_path, monkeypatch):
    """Keep the dispatch tally off the real workspace: tests dispatch freely."""
    from awm.gateway import usage

    usage._reset_for_tests()
    monkeypatch.setattr(usage, "storage_path",
                        lambda: tmp_path / "usage-tally" / "usage.json")
    monkeypatch.setattr(usage, "SEED_PATH", tmp_path / "no-seed.txt")
    yield
    usage._reset_for_tests()


@pytest.fixture(autouse=True)
def _no_extra_roots(monkeypatch):
    """Keep a node's AWM_SERVICE_ROOTS / AWM_PAGE_ROOTS out of discovery tests."""
    monkeypatch.delenv("AWM_SERVICE_ROOTS", raising=False)
    monkeypatch.delenv("AWM_PAGE_ROOTS", raising=False)


@pytest.fixture(autouse=True)
def _no_session_lookup(monkeypatch):
    """Keep the caller lookup off the live gateway: reflection does not answer unless a
    test says so."""
    from awm.gateway import mcp_caller

    def down(pid):
        raise mcp_caller.LookupUnavailable("no reflection in tests")

    monkeypatch.setattr(mcp_caller, "_ask_reflection", down)
    monkeypatch.setattr(mcp_caller, "_cache", {})
