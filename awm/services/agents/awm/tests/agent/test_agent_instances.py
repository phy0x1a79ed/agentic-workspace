"""Tests for awm.agents.agent_instances pure helpers.

Re-homed from awm/tests/agent/test_agent_instances.py for the modular
agents service. Import paths updated; monolith-DB tests removed.

Removed from the original:
  - TestClaudeSessionIdColumn  — tested old agent_sessions table via awm.db
  - TestResumeLookup            — tested old agent_sessions SQL via awm.db
  - TestMigrationFromV23        — tested awm.db migration path (v23→v24)

Kept:
  - TestOpencodeArgv         — pure function, no DB, import-updated
  - TestCreateSessionDispatch — subprocess dispatch, import-updated
"""

from __future__ import annotations

import pytest
pytestmark = [pytest.mark.agent, pytest.mark.slow, pytest.mark.subprocess]

import asyncio
from pathlib import Path

import pytest

import awm.agents.agent_instances as ai_mod
from awm.agents.agent_instances import (
    _SUPPORTED_CLIS,
    _build_opencode_argv,
    _extract_renderable,
    create_session,
)


@pytest.fixture()
def awm_workspace(tmp_path, monkeypatch):
    """Minimal workspace fixture for create_session tests.

    Provides tmp dirs for AWM_DIR and PROJECTS_DIR; patches both
    awm.config and the persistence module so DB ops use tmp_path.
    Also stubs gatewayclient calls (ensureProject/ensureScope) so
    create_session doesn't need a live scopes service.
    """
    import awm.agents.dao as dao_mod
    import awm.persistence.databases as dbs_mod
    import awm.gatewayclient as gw

    awm_dir = tmp_path / ".awm"
    awm_dir.mkdir()
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    monkeypatch.setattr("awm.config.AWM_DIR", awm_dir)
    monkeypatch.setattr("awm.config.PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(ai_mod, "PROJECTS_DIR", projects_dir)
    # ai_mod.config is the imported module; patch its AWM_DIR attr in place.
    import awm.config as _cfg
    monkeypatch.setattr(_cfg, "AWM_DIR", awm_dir)
    monkeypatch.setattr(dbs_mod, "SERVICES_DIR", awm_dir / "services")

    # Stub gatewayclient so ensureProject/ensureScope don't hit the network.
    async def _fake_call(service, fn, args=None, *, as_=None, **kw):
        return {"project": (args or {}).get("project", "p"),
                "scope": (args or {}).get("scope", "s")}
    monkeypatch.setattr(gw, "call", _fake_call)

    # Reset DAO so a fresh DB is created in tmp_path.
    dao_mod._initialized = False
    ai_mod._dao = None
    from awm.agents.dao import init as dao_init
    dao_init()

    yield {
        "awm_dir": awm_dir,
        "projects_dir": projects_dir,
        "workspace": tmp_path,
    }


def test_assistant_text_block_emits_text():
    evt = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "hello"}],
        },
    }
    assert _extract_renderable(evt) == [("message", "hello")]


def test_assistant_tool_use_block_emits_tool_use():
    evt = {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "name": "Read", "id": "x", "input": {}}],
        },
    }
    assert _extract_renderable(evt) == [("tool_use", "[tool_use: Read]")]


def test_user_tool_result_block_emits_tool_result():
    evt = {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "x", "content": "file body"}
            ],
        },
    }
    assert _extract_renderable(evt) == [("tool_result", "file body")]


def test_successful_result_event_is_silent():
    # The success "result" event echoes the final assistant text; the
    # assistant event already posted that, so result must be skipped to
    # avoid duplicate transcript entries.
    evt = {"type": "result", "subtype": "success", "result": "hello", "is_error": False}
    assert _extract_renderable(evt) == []


def test_error_result_event_surfaces_as_system():
    evt = {
        "type": "result",
        "subtype": "error_max_tokens",
        "result": "hit the cap",
        "is_error": True,
    }
    assert _extract_renderable(evt) == [("system", "[error] hit the cap")]


def test_unknown_event_type_is_ignored():
    assert _extract_renderable({"type": "system"}) == []
    assert _extract_renderable({"type": "stream_event", "event": {}}) == []


# ---------------------------------------------------------------------------
# list_sessions limit — "last N" recency cap
# ---------------------------------------------------------------------------

class TestListSessionsLimit:
    def test_limit_caps_to_most_recent(self, awm_workspace):
        """list_sessions returns newest-first (DAO ORDER BY id DESC); limit
        caps to the most recent N — the 'last N sessions' path."""
        dao = ai_mod._get_dao()
        ids = [dao.open_instance(project="awm", scope="dev", log_path=None,
                                 cli_session_id=None, started_at=1000 + i)
               for i in range(4)]

        all_sessions = ai_mod.list_sessions(project="awm", scope="dev")
        assert len(all_sessions) == 4

        recent = ai_mod.list_sessions(project="awm", scope="dev", limit=2)
        assert len(recent) == 2
        # newest-first: the two highest row ids (last inserted)
        assert [s.id for s in recent] == [ids[-1], ids[-2]]


# ---------------------------------------------------------------------------
# OpenCode harness — argv shape + per-session dispatch
# ---------------------------------------------------------------------------


class TestOpencodeArgv:
    """``_build_opencode_argv`` is a pure function — no fixtures needed.
    It produces the argv that ``create_session`` hands to
    ``asyncio.create_subprocess_exec`` when ``agent_cli='opencode'``.
    """

    def test_supported_clis_set(self):
        # The set is consulted in create_session to reject unknown harnesses.
        # If anyone narrows it, opencode/claude-tmux spawning silently raises
        # ValueError for every invite — this guard catches that.
        assert _SUPPORTED_CLIS == {"claude", "claude-tmux", "opencode"}

    def test_basic_shape(self, monkeypatch):
        monkeypatch.setattr(ai_mod, "resolve_bin",
                            lambda name: f"/fake/{name}")
        argv = _build_opencode_argv(
            workspace_dir=Path("/w"), permission_mode="default", model=None,
        )
        assert argv == [
            "/fake/opencode", "run", "--format", "json", "--dir", "/w",
        ]

    def test_bypass_adds_dangerous_flag(self, monkeypatch):
        monkeypatch.setattr(ai_mod, "resolve_bin",
                            lambda name: f"/fake/{name}")
        argv = _build_opencode_argv(
            workspace_dir=Path("/w"),
            permission_mode="bypassPermissions", model=None,
        )
        assert "--dangerously-skip-permissions" in argv
        # Flag appears after --dir <wd>, not as a positional.
        assert argv.index("--dangerously-skip-permissions") > argv.index("--dir")

    def test_default_mode_omits_dangerous_flag(self, monkeypatch):
        monkeypatch.setattr(ai_mod, "resolve_bin",
                            lambda name: f"/fake/{name}")
        argv = _build_opencode_argv(
            workspace_dir=Path("/w"), permission_mode="default", model=None,
        )
        assert "--dangerously-skip-permissions" not in argv

    def test_model_passed_through(self, monkeypatch):
        monkeypatch.setattr(ai_mod, "resolve_bin",
                            lambda name: f"/fake/{name}")
        argv = _build_opencode_argv(
            workspace_dir=Path("/w"), permission_mode="default", model="sonnet",
        )
        assert argv[-2:] == ["--model", "sonnet"]

    def test_resolves_bin_via_resolve_bin(self, monkeypatch):
        captured = []
        def fake_resolve(name):
            captured.append(name)
            return f"/usr/local/bin/{name}"
        monkeypatch.setattr(ai_mod, "resolve_bin", fake_resolve)

        argv = _build_opencode_argv(
            workspace_dir=Path("/w"), permission_mode="default", model=None,
        )
        assert captured == ["opencode"]
        assert argv[0] == "/usr/local/bin/opencode"


# ---------------------------------------------------------------------------
# create_session now drives an agentcore AgentSession — _build_core_config
# maps the spawn args onto an AgentConfig and open_agent() selects the backend.
# The subprocess + stream parsing live in agentcore (tested in that dist); here
# we pin that create_session builds the right AgentConfig and wires it up.
# ---------------------------------------------------------------------------


class _FakeCoreSession:
    """Stand-in for an agentcore AgentSession.

    Records sends, exposes a no-op proc, and yields no events (its subscribe
    stream ends immediately so _reader_loop returns). The reader/waiter loops
    run for real against it — verifying the wiring without a subprocess."""

    def __init__(self):
        self.config = None
        self.sent: list[str] = []
        self.started = False
        self.closed = False
        self._proc = _FakeProc()

    def subscribe(self):
        async def _gen():
            if False:
                yield  # pragma: no cover — empty async iterator
        return _gen()

    async def start(self):
        self.started = True

    async def send(self, text):
        self.sent.append(text)

    async def wait(self):
        # Delegates to the proc (blocks forever) so the rewired _waiter_loop
        # doesn't tear the session down mid-test — matches the prior behavior
        # when the loop awaited session.proc.wait() directly.
        return await self._proc.wait()

    def alive(self):
        return self._proc.returncode is None

    async def close(self):
        self.closed = True


class _FakeProc:
    def __init__(self, pid=4321):
        self.pid = pid
        self.returncode = None
    async def wait(self):
        # Never return so _waiter_loop doesn't tear the session down mid-test.
        import asyncio as _a
        await _a.Event().wait()


@pytest.fixture()
def stub_agentcore(monkeypatch):
    """Patch open_agent to capture the AgentConfig + return a fake session.

    Resets the registry between tests so the per-scope uniqueness check doesn't
    leak across cases. Returns the dict holding the last-built config + session.
    """
    captured: dict = {}

    def fake_open_agent(config):
        sess = _FakeCoreSession()
        sess.config = config
        captured["config"] = config
        captured["session"] = sess
        return sess

    monkeypatch.setattr(ai_mod, "open_agent", fake_open_agent)
    ai_mod._registry_by_id.clear()
    ai_mod._by_scope.clear()
    yield captured
    ai_mod._registry_by_id.clear()
    ai_mod._by_scope.clear()


class TestCreateSessionDispatch:
    """``create_session`` builds an :class:`AgentConfig` and drives an agentcore
    session. These tests pin that config-mapping contract."""

    @pytest.mark.asyncio
    async def test_rejects_unknown_agent_cli(self, awm_workspace):
        with pytest.raises(ValueError, match="Unknown agent CLI"):
            await create_session(project="p", scope="s", agent_cli="codex")

    @pytest.mark.asyncio
    async def test_opencode_config_harness_and_workdir(
        self, awm_workspace, stub_agentcore,
    ):
        ws = awm_workspace["projects_dir"] / "p" / "s"
        ws.mkdir(parents=True)

        await create_session(project="p", scope="s", agent_cli="opencode")

        cfg = stub_agentcore["config"]
        assert cfg.harness == "opencode"
        assert cfg.mode == "live"
        assert cfg.workdir == str(ws)

    @pytest.mark.asyncio
    async def test_bypass_maps_to_full_permissions(
        self, awm_workspace, stub_agentcore,
    ):
        ws = awm_workspace["projects_dir"] / "p" / "s"
        ws.mkdir(parents=True)

        await create_session(project="p", scope="s", agent_cli="claude",
                             permission_mode="bypassPermissions")

        assert stub_agentcore["config"].permissions == "full"

    @pytest.mark.asyncio
    async def test_default_mode_maps_to_default_permissions(
        self, awm_workspace, stub_agentcore,
    ):
        ws = awm_workspace["projects_dir"] / "p" / "s"
        ws.mkdir(parents=True)

        await create_session(project="p", scope="s", agent_cli="claude",
                             permission_mode="default")

        assert stub_agentcore["config"].permissions == "default"

    @pytest.mark.asyncio
    async def test_claude_threads_spawn_mcp_config(
        self, awm_workspace, stub_agentcore,
    ):
        ws = awm_workspace["projects_dir"] / "p" / "s"
        ws.mkdir(parents=True)
        spawn_mcp = awm_workspace["awm_dir"] / "spawn-mcp.json"
        spawn_mcp.write_text('{"mcpServers": {}}')

        await create_session(project="p", scope="s", agent_cli="claude")

        assert stub_agentcore["config"].mcp_config == str(spawn_mcp)

    @pytest.mark.asyncio
    async def test_effort_rides_params(
        self, awm_workspace, stub_agentcore,
    ):
        ws = awm_workspace["projects_dir"] / "p" / "s"
        ws.mkdir(parents=True)

        await create_session(project="p", scope="s", agent_cli="claude",
                             effort="high")

        assert stub_agentcore["config"].params.get("effort") == "high"


class TestClaudeTmuxHarness:
    """The agents service accepts the ``claude-tmux`` harness and records the
    deterministic, human-attachable tmux session name."""

    @pytest.mark.asyncio
    async def test_accepted_and_harness_threaded(
        self, awm_workspace, stub_agentcore,
    ):
        ws = awm_workspace["projects_dir"] / "p" / "s"
        ws.mkdir(parents=True)

        session = await create_session(
            project="p", scope="s", agent_cli="claude-tmux")

        cfg = stub_agentcore["config"]
        assert cfg.harness == "claude-tmux"
        # deterministic name: awm-<instance_id>-<scope>
        assert cfg.tmux_session_name == f"awm-{session.id}-s"
        assert session.tmux_session == f"awm-{session.id}-s"

    @pytest.mark.asyncio
    async def test_records_tmux_session_and_cli_in_data(
        self, awm_workspace, stub_agentcore,
    ):
        ws = awm_workspace["projects_dir"] / "p" / "s"
        ws.mkdir(parents=True)

        session = await create_session(
            project="p", scope="s", agent_cli="claude-tmux")

        import json
        row = ai_mod._get_dao().get_instance(session.id)
        data = json.loads(row["data"])
        assert data["agent_cli"] == "claude-tmux"
        assert data["tmux_session"] == f"awm-{session.id}-s"
        # and it surfaces through the listing info
        info = ai_mod._info_for_instance_row(ai_mod._row_for_instance(session.id))
        assert info.tmux_session == f"awm-{session.id}-s"
        assert info.agent_cli == "claude-tmux"

    @pytest.mark.asyncio
    async def test_threads_spawn_mcp_config(
        self, awm_workspace, stub_agentcore,
    ):
        ws = awm_workspace["projects_dir"] / "p" / "s"
        ws.mkdir(parents=True)
        spawn_mcp = awm_workspace["awm_dir"] / "spawn-mcp.json"
        spawn_mcp.write_text('{"mcpServers": {}}')

        await create_session(project="p", scope="s", agent_cli="claude-tmux")

        # claude-tmux gets the same spawn-mcp.json claude does.
        assert stub_agentcore["config"].mcp_config == str(spawn_mcp)


class TestBuildCoreConfig:
    """``_build_core_config`` — the pure spawn-args → AgentConfig mapping."""

    def test_full_open_and_model(self, awm_workspace):
        ws = awm_workspace["projects_dir"] / "p" / "s"
        cfg = ai_mod._build_core_config(
            agent_cli="claude", permission_mode="bypassPermissions",
            model="opus", effort=None, resume_session_id="sid-1",
            workspace_dir=ws, awm_dir=ws / ".awm",
        )
        assert cfg.harness == "claude"
        assert cfg.permissions == "full"
        assert cfg.model == "opus"
        assert cfg.resume_id == "sid-1"
        assert cfg.workdir == str(ws)

    def test_opencode_no_claude_mcp(self, awm_workspace):
        ws = awm_workspace["projects_dir"] / "p" / "s"
        # Even with a spawn-mcp.json present, opencode doesn't thread it
        # (claude-only flag).
        (awm_workspace["awm_dir"] / "spawn-mcp.json").write_text("{}")
        cfg = ai_mod._build_core_config(
            agent_cli="opencode", permission_mode="default",
            model=None, effort=None, resume_session_id=None,
            workspace_dir=ws, awm_dir=ws / ".awm",
        )
        assert cfg.mcp_config is None
        assert cfg.permissions == "default"
