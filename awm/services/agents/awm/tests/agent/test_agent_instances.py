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
        # If anyone narrows it back to {"claude"}, opencode spawning silently
        # raises ValueError for every invite — this guard catches that.
        assert _SUPPORTED_CLIS == {"claude", "opencode"}

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


class _FakeStream:
    """Stand-in for the ``.stdin``/``.stdout`` attrs of a real Process.
    The reader/pump loops are patched to no-ops, so these never get used —
    but ``AgentInstance.__init__`` reads ``proc.stdin`` etc. for assignment."""
    def __init__(self):
        self._closing = False
    def is_closing(self): return self._closing
    def write(self, _data): pass
    async def drain(self): pass


class _FakeProc:
    def __init__(self, pid=4321):
        self.pid = pid
        self.stdin = _FakeStream()
        self.stdout = _FakeStream()
        self.returncode = None
    async def wait(self): return 0


@pytest.fixture()
def stub_subprocess(monkeypatch):
    """Capture asyncio.create_subprocess_exec args and return a fake proc.

    Also stubs out the three lifecycle pumps (reader/waiter/input) so they
    don't block on the fake streams. Resets the registry between tests so
    the per-scope uniqueness check doesn't leak across cases.
    """
    calls = {}

    async def fake_exec(*argv, **kwargs):
        calls["argv"] = list(argv)
        calls["env"] = kwargs.get("env")
        calls["cwd"] = kwargs.get("cwd")
        return _FakeProc()

    async def _noop(_session): return None

    async def _stdin_ready_pump(session):
        # The real _input_pump verifies proc.stdin is usable, then sets
        # stdin_ready so create_session can return. Mirror that single
        # behavior so the unconditional `stdin_ready.wait()` at the end
        # of create_session doesn't time out (and try to kill the fake).
        session.stdin_ready.set()

    monkeypatch.setattr(ai_mod.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(ai_mod, "_reader_loop", _noop)
    monkeypatch.setattr(ai_mod, "_waiter_loop", _noop)
    monkeypatch.setattr(ai_mod, "_input_pump", _stdin_ready_pump)
    monkeypatch.setattr(ai_mod, "resolve_bin",
                        lambda name: f"/fake/bin/{name}")
    # PROJECTS_DIR is a name-imported binding on agent_instances; if the
    # awm_workspace fixture already patched it, don't re-patch here (the
    # awm_workspace fixture's tmp_path version must win).
    pass
    # Clear the per-scope guard so each test starts clean.
    ai_mod._registry_by_id.clear()
    ai_mod._by_scope.clear()
    yield calls
    ai_mod._registry_by_id.clear()
    ai_mod._by_scope.clear()


class TestCreateSessionDispatch:
    """``create_session`` branches on ``agent_cli`` to choose argv shape and
    env. These tests pin that dispatch contract."""

    @pytest.mark.asyncio
    async def test_rejects_unknown_agent_cli(self, awm_workspace):
        with pytest.raises(ValueError, match="Unknown agent CLI"):
            await create_session(project="p", scope="s", agent_cli="codex")

    @pytest.mark.asyncio
    async def test_opencode_branch_argv_and_env(self, awm_workspace,
                                                  stub_subprocess):
        # Scope workspace must exist for create_session to proceed.
        ws = awm_workspace["projects_dir"] / "p" / "s"
        ws.mkdir(parents=True)
        # OPENCODE_CONFIG gets injected only when the exporter actually
        # wrote the file — simulate that.
        cfg = awm_workspace["awm_dir"] / "mcp-opencode.json"
        cfg.write_text('{"mcp": {}}')

        await create_session(project="p", scope="s", agent_cli="opencode")

        argv = stub_subprocess["argv"]
        assert argv[0] == "/fake/bin/opencode"
        assert "run" in argv
        assert "--dir" in argv
        assert argv[argv.index("--dir") + 1] == str(ws)
        env = stub_subprocess["env"]
        assert env is not None
        assert env["OPENCODE_CONFIG"] == str(cfg)
        # cwd matches workspace dir.
        assert stub_subprocess["cwd"] == str(ws)

    @pytest.mark.asyncio
    async def test_opencode_branch_no_env_when_config_missing(
        self, awm_workspace, stub_subprocess,
    ):
        ws = awm_workspace["projects_dir"] / "p" / "s"
        ws.mkdir(parents=True)
        # No mcp-opencode.json in AWM_DIR — env stays None (inherit parent).
        await create_session(project="p", scope="s", agent_cli="opencode")
        assert stub_subprocess["env"] is None
        assert stub_subprocess["argv"][0] == "/fake/bin/opencode"

    @pytest.mark.asyncio
    async def test_claude_branch_does_not_set_opencode_env(
        self, awm_workspace, stub_subprocess,
    ):
        ws = awm_workspace["projects_dir"] / "p" / "s"
        ws.mkdir(parents=True)
        # Even if an opencode config exists, the claude branch must not
        # leak OPENCODE_CONFIG into its env.
        (awm_workspace["awm_dir"] / "mcp-opencode.json").write_text("{}")

        await create_session(project="p", scope="s", agent_cli="claude")

        argv = stub_subprocess["argv"]
        assert argv[0] == "/fake/bin/claude"
        # Claude harness inherits parent env (env=None).
        assert stub_subprocess["env"] is None

    @pytest.mark.asyncio
    async def test_opencode_prefers_per_scope_config(
        self, awm_workspace, stub_subprocess,
    ):
        # Per-scope `<worktree>/.awm/mcp-opencode.json` wins over the
        # workspace-level fallback — that's how `instructions` for the
        # current scope's `.awm/context.md` reach opencode.
        ws = awm_workspace["projects_dir"] / "p" / "s"
        ws.mkdir(parents=True)
        scope_awm = ws / ".awm"
        scope_awm.mkdir()
        scope_cfg = scope_awm / "mcp-opencode.json"
        scope_cfg.write_text('{"mcp": {}, "instructions": [".awm/context.md"]}')
        # Workspace fallback also present — should NOT be used.
        workspace_cfg = awm_workspace["awm_dir"] / "mcp-opencode.json"
        workspace_cfg.write_text('{"mcp": {"awm": {"type": "local"}}}')

        await create_session(project="p", scope="s", agent_cli="opencode")

        env = stub_subprocess["env"]
        assert env is not None
        assert env["OPENCODE_CONFIG"] == str(scope_cfg)
