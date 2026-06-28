"""Tests for awm.agents.agent_instances pure helpers.

Re-homed from awm/tests/agent/test_agent_instances.py for the modular
agents service. Import paths updated; monolith-DB tests removed.

Removed from the original:
  - TestClaudeSessionIdColumn  — tested old agent_sessions table via awm.db
  - TestResumeLookup            — tested old agent_sessions SQL via awm.db
  - TestMigrationFromV23        — tested awm.db migration path (v23→v24)

Kept:
  - TestSupportedClis         — the harness allowlist guard
  - TestCreateSessionDispatch — subprocess dispatch, import-updated
"""

from __future__ import annotations

import pytest
pytestmark = [pytest.mark.agent, pytest.mark.slow, pytest.mark.subprocess]

import asyncio

import pytest

import awm.agents.agent_instances as ai_mod
from awm.agents.agent_instances import (
    _SUPPORTED_CLIS,
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


class TestSupportedClis:
    """The harness allowlist consulted by ``create_session``."""

    def test_supported_clis_set(self):
        # The set is consulted in create_session to reject unknown harnesses.
        # claude (the tmux TUI) + opencode; the legacy "claude-tmux" name folds
        # onto "claude" at hydration, not here.
        assert _SUPPORTED_CLIS == {"claude", "opencode"}


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
        self.interrupted: list[str] = []
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

    async def interrupt(self, text):
        # Mirror the real AgentSession seam: capture the forced-interrupt.
        self.interrupted.append(text)

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

        await create_session(project="p", scope="s", agent_cli="opencode",
                             workdir=str(ws))

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
                             permission_mode="bypassPermissions",
                             workdir=str(ws))

        assert stub_agentcore["config"].permissions == "full"

    @pytest.mark.asyncio
    async def test_default_mode_maps_to_default_permissions(
        self, awm_workspace, stub_agentcore,
    ):
        ws = awm_workspace["projects_dir"] / "p" / "s"
        ws.mkdir(parents=True)

        await create_session(project="p", scope="s", agent_cli="claude",
                             permission_mode="default", workdir=str(ws))

        assert stub_agentcore["config"].permissions == "default"

    @pytest.mark.asyncio
    async def test_threads_hub_workspace_and_port_for_harness_mcp(
        self, awm_workspace, stub_agentcore,
    ):
        # MCP setup is harness-owned now: create_session does NOT write or thread
        # a config FILE; it passes the hub's canonical workspace + port so the
        # harness synthesizes the awm server itself. Every session is a placement,
        # so it carries its AWM_AS identity ("project/scope").
        ws = awm_workspace["projects_dir"] / "p" / "s"
        ws.mkdir(parents=True)

        await create_session(project="p", scope="s", agent_cli="claude",
                             workdir=str(ws))

        cfg = stub_agentcore["config"]
        assert cfg.mcp_config is None
        assert cfg.awm_port == str(ai_mod.config.PORT)
        assert cfg.awm_workspace == str(ai_mod.config.canonical_workspace())
        assert cfg.placement_as == "p/s"

    @pytest.mark.asyncio
    async def test_effort_rides_params(
        self, awm_workspace, stub_agentcore,
    ):
        ws = awm_workspace["projects_dir"] / "p" / "s"
        ws.mkdir(parents=True)

        await create_session(project="p", scope="s", agent_cli="claude",
                             effort="high", workdir=str(ws))

        assert stub_agentcore["config"].params.get("effort") == "high"


class TestClaudeHarness:
    """A claude agent runs in tmux: the service records the deterministic,
    human-attachable tmux session name."""

    @pytest.mark.asyncio
    async def test_accepted_and_harness_threaded(
        self, awm_workspace, stub_agentcore,
    ):
        ws = awm_workspace["projects_dir"] / "p" / "s"
        ws.mkdir(parents=True)

        session = await create_session(
            project="p", scope="s", agent_cli="claude", workdir=str(ws))

        cfg = stub_agentcore["config"]
        assert cfg.harness == "claude"
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
            project="p", scope="s", agent_cli="claude", workdir=str(ws))

        import json
        row = ai_mod._get_dao().get_instance(session.id)
        data = json.loads(row["data"])
        assert data["agent_cli"] == "claude"
        assert data["tmux_session"] == f"awm-{session.id}-s"
        # and it surfaces through the listing info
        info = ai_mod._info_for_instance_row(ai_mod._row_for_instance(session.id))
        assert info.tmux_session == f"awm-{session.id}-s"
        assert info.agent_cli == "claude"

    @pytest.mark.asyncio
    async def test_claude_is_the_default_harness(
        self, awm_workspace, stub_agentcore,
    ):
        ws = awm_workspace["projects_dir"] / "p" / "s"
        ws.mkdir(parents=True)

        # No agent_cli passed → defaults to claude (tmux) so every live agent
        # has a terminal to attach to.
        session = await create_session(project="p", scope="s", workdir=str(ws))

        assert stub_agentcore["config"].harness == "claude"
        assert session.tmux_session == f"awm-{session.id}-s"

    def test_legacy_claude_tmux_alias_folds_to_claude(self):
        # A persisted/explicit legacy "claude-tmux" name reads back as "claude".
        assert ai_mod._normalize_cli("claude-tmux") == "claude"
        assert ai_mod._normalize_cli("claude") == "claude"
        assert ai_mod._normalize_cli(None) == "claude"


class TestBuildCoreConfig:
    """``_build_core_config`` — the pure spawn-args → AgentConfig mapping."""

    def test_full_open_and_model(self, awm_workspace):
        ws = awm_workspace["projects_dir"] / "p" / "s"
        cfg = ai_mod._build_core_config(
            agent_cli="claude", permission_mode="bypassPermissions",
            model="opus", effort=None, resume_session_id="sid-1",
            workspace_dir=ws,
        )
        assert cfg.harness == "claude"
        assert cfg.permissions == "full"
        assert cfg.model == "opus"
        assert cfg.resume_id == "sid-1"
        assert cfg.workdir == str(ws)
        # MCP setup is harness-owned: the hub workspace + port are threaded so
        # the harness can synthesize the awm server. No config FILE is written
        # here, and a conversational session carries no AWM_AS identity.
        assert cfg.awm_port == str(ai_mod.config.PORT)
        assert cfg.awm_workspace == str(ai_mod.config.canonical_workspace())
        assert cfg.placement_as is None
        assert cfg.mcp_config is None

    def test_placement_identity_threaded(self, awm_workspace):
        ws = awm_workspace["projects_dir"] / "p" / "s"
        # A placement passes its identity so the harness synthesizes an awm
        # server carrying AWM_AS — symmetric across claude and opencode.
        for harness in ("claude", "opencode"):
            cfg = ai_mod._build_core_config(
                agent_cli=harness, permission_mode="default",
                model=None, effort=None, resume_session_id=None,
                workspace_dir=ws, placement_as="p/s",
            )
            assert cfg.placement_as == "p/s"
            assert cfg.awm_port == str(ai_mod.config.PORT)
            assert cfg.mcp_config is None


class TestNotifyAgent:
    """The forced-interrupt notification channel: preempts the current turn via
    the harness interrupt seam, filters the agent's own posts, and is distinct
    from the passive (turn-aligned) enqueue_input path."""

    @pytest.mark.asyncio
    async def test_notify_interrupts_not_enqueues(
        self, awm_workspace, stub_agentcore,
    ):
        ws = awm_workspace["projects_dir"] / "p" / "s"
        ws.mkdir(parents=True)
        session = await create_session(project="p", scope="s", workdir=str(ws))

        ok = await ai_mod.notify_agent(session, "operator", "stop and read this")

        assert ok is True
        core = stub_agentcore["session"]
        # Went through interrupt (forced), NOT the passive send/enqueue queue.
        assert core.interrupted == ["[notify:operator]\nstop and read this"]
        assert core.sent == []

    @pytest.mark.asyncio
    async def test_notify_filters_agent_own_author(
        self, awm_workspace, stub_agentcore,
    ):
        ws = awm_workspace["projects_dir"] / "p" / "s"
        ws.mkdir(parents=True)
        session = await create_session(project="p", scope="s", workdir=str(ws))

        # The agent's own scope ref must never self-notify (mirrors the passive
        # path's _is_own_author guard).
        ok = await ai_mod.notify_agent(session, "agent:p/s", "echo")

        assert ok is False
        assert stub_agentcore["session"].interrupted == []
