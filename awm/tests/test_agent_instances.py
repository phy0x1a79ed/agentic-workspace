"""Tests for awm.services.agent_instances pure helpers.

The AgentInstance lifecycle is integration-heavy (spawns claude); these tests
cover the stream-json parsing in _extract_renderable plus the DB-level
persistence of claude_session_id that lets re-invite-after-death still
resume the same claude conversation.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from awm.db import get_connection
import awm.services.agent_instances as ai_mod
from awm.services.agent_instances import (
    _SUPPORTED_CLIS,
    _build_opencode_argv,
    _extract_renderable,
    create_session,
)


def test_assistant_text_block_emits_text():
    evt = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "hello"}],
        },
    }
    assert _extract_renderable(evt) == [("text", "hello")]


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
# claude_session_id persistence + resume-after-reap
# ---------------------------------------------------------------------------


def _insert_session_row(conn: sqlite3.Connection, *,
                        project: str, scope: str, status: str,
                        claude_session_id: str | None,
                        started_at: str = "2026-05-21T00:00:00+00:00") -> int:
    cur = conn.execute(
        "INSERT INTO agent_sessions "
        "(project, scope, pid, status, agent_cli, started_at, log_path, "
        " claude_session_id) "
        "VALUES (?, ?, 0, ?, 'claude', ?, '/tmp/log', ?)",
        (project, scope, status, started_at, claude_session_id),
    )
    conn.commit()
    return cur.lastrowid


class TestClaudeSessionIdColumn:
    def test_column_exists_after_init(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        cols = {r["name"]
                for r in conn.execute("PRAGMA table_info(agent_sessions)")}
        conn.close()
        assert "claude_session_id" in cols

    def test_can_store_and_read_id(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        sid = _insert_session_row(
            conn, project="p", scope="s", status="exited",
            claude_session_id="abc-123",
        )
        row = conn.execute(
            "SELECT claude_session_id FROM agent_sessions WHERE id=?", (sid,),
        ).fetchone()
        conn.close()
        assert row["claude_session_id"] == "abc-123"


class TestResumeLookup:
    """The SQL the create_session path runs to recover a resume id when no
    in-memory predecessor exists. Asserting the query directly (rather than
    driving it through create_session, which would spawn claude) keeps the
    test hermetic."""

    LOOKUP_SQL = (
        "SELECT claude_session_id FROM agent_sessions "
        "WHERE project=? AND scope=? AND claude_session_id IS NOT NULL "
        "ORDER BY id DESC LIMIT 1"
    )

    def test_returns_most_recent_id(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        _insert_session_row(conn, project="p", scope="s", status="exited",
                            claude_session_id="old-id")
        _insert_session_row(conn, project="p", scope="s", status="exited",
                            claude_session_id="new-id")
        row = conn.execute(self.LOOKUP_SQL, ("p", "s")).fetchone()
        conn.close()
        assert row is not None
        assert row["claude_session_id"] == "new-id"

    def test_skips_rows_with_no_id(self, awm_workspace):
        # Re-invite after the agent died before it ever emitted an init
        # event — there's a row but no captured id. Lookup must skip it.
        conn = get_connection(awm_workspace["db_path"])
        _insert_session_row(conn, project="p", scope="s", status="exited",
                            claude_session_id="real-id")
        _insert_session_row(conn, project="p", scope="s", status="exited",
                            claude_session_id=None)
        row = conn.execute(self.LOOKUP_SQL, ("p", "s")).fetchone()
        conn.close()
        assert row is not None
        assert row["claude_session_id"] == "real-id"

    def test_no_history_returns_none(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        row = conn.execute(self.LOOKUP_SQL, ("p", "s")).fetchone()
        conn.close()
        assert row is None

    def test_other_scope_does_not_leak(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        _insert_session_row(conn, project="p", scope="other", status="exited",
                            claude_session_id="other-id")
        row = conn.execute(self.LOOKUP_SQL, ("p", "s")).fetchone()
        conn.close()
        assert row is None


class TestMigrationFromV23:
    def test_v23_db_gets_column(self, tmp_path, monkeypatch):
        """A pre-existing v23 DB (without the column) should pick up
        claude_session_id when init_db migrates it forward."""
        from awm.db import init_db, SCHEMA_VERSION
        assert SCHEMA_VERSION >= 24, "this test guards the v23→v24 migration"

        db_path = tmp_path / "v23.db"
        monkeypatch.setattr("awm.db.AWM_DIR", tmp_path)

        # Build a v23-shaped agent_sessions (no claude_session_id) and
        # claim schema_version=23 so _migrate runs (23, 24).
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            CREATE TABLE agent_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project TEXT NOT NULL,
                scope TEXT NOT NULL,
                pid INTEGER NOT NULL,
                status TEXT NOT NULL,
                agent_cli TEXT NOT NULL,
                started_at TEXT NOT NULL,
                exited_at TEXT,
                exit_code INTEGER,
                log_path TEXT NOT NULL
            );
            INSERT INTO schema_version (version) VALUES (23);
            INSERT INTO agent_sessions
                (project, scope, pid, status, agent_cli, started_at, log_path)
                VALUES ('p', 's', 0, 'exited', 'claude',
                        '2026-05-21T00:00:00+00:00', '/tmp/log');
        """)
        conn.commit()
        conn.close()

        init_db(db_path)

        conn = get_connection(db_path)
        try:
            cols = {r["name"]
                    for r in conn.execute("PRAGMA table_info(agent_sessions)")}
            assert "claude_session_id" in cols
            row = conn.execute(
                "SELECT claude_session_id FROM agent_sessions WHERE project='p'"
            ).fetchone()
            assert row["claude_session_id"] is None
            _insert_session_row(conn, project="p", scope="s2",
                                status="exited", claude_session_id="x")
            row = conn.execute(
                "SELECT claude_session_id FROM agent_sessions WHERE scope='s2'"
            ).fetchone()
            assert row["claude_session_id"] == "x"
        finally:
            conn.close()


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

    monkeypatch.setattr(ai_mod.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(ai_mod, "_reader_loop", _noop)
    monkeypatch.setattr(ai_mod, "_waiter_loop", _noop)
    monkeypatch.setattr(ai_mod, "_input_pump", _noop)
    monkeypatch.setattr(ai_mod, "resolve_bin",
                        lambda name: f"/fake/bin/{name}")
    # PROJECTS_DIR and config.AWM_DIR were name-imported / module-attr-read
    # into agent_instances; the base awm_workspace fixture patches the
    # canonical config but not the per-module bindings here.
    from awm import config as _cfg
    yield_workspace = getattr(_cfg, "WORKSPACE_ROOT", None)
    # config.AWM_DIR is read via attribute — patching awm.config.AWM_DIR
    # (done by awm_workspace) already covers it. PROJECTS_DIR is a direct
    # `from awm.config import PROJECTS_DIR` so its binding must be patched
    # on the agent_instances module.
    if yield_workspace is not None:
        monkeypatch.setattr(ai_mod, "PROJECTS_DIR",
                            yield_workspace / "projects")
    # Clear the per-scope guard so each test starts clean.
    ai_mod._registry.clear()
    ai_mod._by_scope.clear()
    yield calls
    ai_mod._registry.clear()
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
