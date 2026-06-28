"""Unit tests for the tmux-driven interactive claude backend.

All tests here run with NO real tmux or claude — tmux invocations are mocked at
the subprocess boundary, and the transcript-tail loop is driven against temp
files. The one live multi-turn test lives in ``test_live_backends.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

from awm.agentcore import AgentConfig, open_agent
from awm.agentcore.claude_backend import (
    ClaudeSession,
    build_claude_argv,
    _hook_settings,
    _launcher_script,
    _safe_json,
)


# ---------------------------------------------------------------------------
# dispatch + argv
# ---------------------------------------------------------------------------

def test_open_agent_dispatches_tmux():
    s = open_agent(AgentConfig(harness="claude", workdir="/tmp"))
    assert isinstance(s, ClaudeSession)


def test_argv_interactive_full_perms():
    cfg = AgentConfig(harness="claude", model="haiku", permissions="full")
    argv = build_claude_argv(cfg, "/bin/claude", "SID", "/s.json")
    # interactive: NONE of the headless stream-json flags
    for bad in ("--print", "--output-format=stream-json",
                "--input-format=stream-json", "--include-partial-messages"):
        assert bad not in argv
    assert "--dangerously-skip-permissions" in argv
    assert ["--model", "haiku"] == argv[argv.index("--model"):argv.index("--model") + 2]
    assert ["--settings", "/s.json"] == argv[-4:-2]
    assert ["--session-id", "SID"] == argv[-2:]


def test_argv_default_perms_and_resume():
    cfg = AgentConfig(harness="claude", permissions="default", resume_id="R1")
    argv = build_claude_argv(cfg, "/bin/claude", "SID", "/s.json")
    assert "--dangerously-skip-permissions" not in argv
    assert ["--permission-mode", "default"] == argv[1:3]
    # resume wins over a minted session id
    assert "--resume" in argv and "R1" in argv
    assert "--session-id" not in argv


def test_argv_tools_effort_mcp(tmp_path):
    mcp = tmp_path / "mcp.json"
    mcp.write_text("{}")
    cfg = AgentConfig(
        harness="claude", permissions="full",
        params={"effort": "high"},
        system_prompt="be terse",
        allowed_tools=["Read", "mcp__awm__edit_deliverable"],
        disallowed_tools=["Bash"],
        mcp_config=str(mcp),
    )
    argv = build_claude_argv(cfg, "/bin/claude", "SID", "/s.json")
    assert ["--effort", "high"] == argv[argv.index("--effort"):argv.index("--effort") + 2]
    assert "--append-system-prompt" in argv and "be terse" in argv
    assert "Read,mcp__awm__edit_deliverable" in argv
    assert argv[argv.index("--disallowedTools") + 1] == "Bash"
    assert "--strict-mcp-config" in argv and str(mcp) in argv


def test_argv_bad_effort_raises():
    cfg = AgentConfig(harness="claude", params={"effort": "ludicrous"})
    with pytest.raises(ValueError):
        build_claude_argv(cfg, "/bin/claude", "SID", "/s.json")


# ---------------------------------------------------------------------------
# settings + launcher contents
# ---------------------------------------------------------------------------

def test_hook_settings_shape():
    s = _hook_settings("/abs/_hook_emit.py")
    hooks = s["hooks"]
    assert set(hooks) == {"SessionStart", "Stop"}
    cmd = hooks["Stop"][0]["hooks"][0]["command"]
    assert cmd.endswith("_hook_emit.py") or "_hook_emit.py" in cmd
    assert sys.executable in cmd  # absolute interpreter, not bare python3


def test_launcher_exports_control_and_execs():
    script = _launcher_script("/tmp/ctrl.jsonl", ["/bin/claude", "--flag", "v a l"])
    assert "export AWM_AGENTCORE_CONTROL_FILE=" in script
    assert "/tmp/ctrl.jsonl" in script
    assert script.splitlines()[0] == "#!/usr/bin/env bash"
    assert "exec /bin/claude --flag 'v a l'" in script  # shlex-quoted argv


def test_safe_json():
    assert _safe_json('{"a":1}') == {"a": 1}
    assert _safe_json("") is None
    assert _safe_json("not json") is None
    assert _safe_json("[1,2]") is None  # only dicts


# ---------------------------------------------------------------------------
# transcript line classification → stable ids
# ---------------------------------------------------------------------------

def _sess() -> ClaudeSession:
    return ClaudeSession(AgentConfig(harness="claude", workdir="/tmp"))


def test_classify_assistant_text_line():
    s = _sess()
    line = json.dumps({
        "type": "assistant", "uuid": "u1",
        "message": {"id": "msg_1", "content": [{"type": "text", "text": "hello"}]},
    })
    evs = s._classify_line(line)
    assert len(evs) == 1
    assert evs[0].kind == "message" and evs[0].text == "hello"
    assert evs[0].id == "u1#0"  # transcript-stable id
    assert evs[0].data["message_id"] == "msg_1"


def test_classify_tool_use_line():
    s = _sess()
    line = json.dumps({
        "type": "assistant", "uuid": "u2",
        "message": {"id": "m2", "content": [
            {"type": "text", "text": "running"},
            {"type": "tool_use", "name": "Bash", "id": "t1", "input": {"command": "ls"}},
        ]},
    })
    evs = s._classify_line(line)
    kinds = [(e.kind, e.id) for e in evs]
    assert kinds == [("message", "u2#0"), ("tool_use", "u2#1")]


def test_classify_user_text_is_noop():
    # A plain user prompt has string content → no event (don't echo the prompt).
    s = _sess()
    line = json.dumps({"type": "user", "uuid": "u3",
                       "message": {"content": "line one\nline two"}})
    assert s._classify_line(line) == []


def test_classify_unknown_types_noop():
    s = _sess()
    for t in ("mode", "permission-mode", "file-history-snapshot",
              "attachment", "ai-title", "system"):
        line = json.dumps({"type": t, "uuid": "x"})
        assert s._classify_line(line) == []
    assert s._classify_line("garbage") == []


def test_classify_tool_result_line():
    s = _sess()
    line = json.dumps({
        "type": "user", "uuid": "u4",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "done", "is_error": False},
        ]},
    })
    evs = s._classify_line(line)
    assert len(evs) == 1 and evs[0].kind == "tool_result"
    assert evs[0].id == "u4#0"


# ---------------------------------------------------------------------------
# the control-file + transcript tail loop (no tmux; real files)
# ---------------------------------------------------------------------------

async def test_event_source_tail(tmp_path):
    """Drive the real _event_source against temp control + transcript files:
    SessionStart → status(+path), transcript lines → events, Stop → result."""
    control = tmp_path / "control.jsonl"
    transcript = tmp_path / "transcript.jsonl"
    control.write_text("")
    # NOTE: transcript is intentionally NOT created up front — claude creates it
    # lazily (only on first activity). The tail must wait for it past any fixed
    # window, so the first append below is what brings it into existence.

    s = _sess()
    s._control_path = str(control)
    s._started = True  # skip start(); we feed files directly

    collected = []

    async def consume():
        async for ev in s._event_source():
            collected.append(ev)
            if ev.kind == "result":
                return

    task = asyncio.create_task(consume())

    def append(path, obj):
        with open(path, "a") as f:
            f.write(json.dumps(obj) + "\n")

    # 1. SessionStart hands the transcript path.
    await asyncio.sleep(0.05)
    append(control, {"event": "SessionStart", "transcript_path": str(transcript),
                     "session_id": "SID-9"})
    await asyncio.sleep(0.2)
    # 2. assistant turn writes transcript lines.
    append(transcript, {"type": "assistant", "uuid": "a1",
                        "message": {"id": "m", "content": [{"type": "text", "text": "PONG"}]}})
    await asyncio.sleep(0.2)
    # 3. Stop closes the turn.
    append(control, {"event": "Stop", "transcript_path": str(transcript)})

    await asyncio.wait_for(task, timeout=5)
    await s.close()

    kinds = [e.kind for e in collected]
    assert kinds[0] == "status"
    assert collected[0].data["transcript_path"] == str(transcript)
    assert collected[0].data["session_id"] == "SID-9"
    assert "message" in kinds
    msg = next(e for e in collected if e.kind == "message")
    assert msg.text == "PONG"
    result = collected[-1]
    assert result.kind == "result"
    assert result.text == "PONG"  # last assistant text rides the boundary
    assert result.data["turn_boundary"] is True


# ---------------------------------------------------------------------------
# send() command sequence (mocked subprocess)
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self):
        self.returncode = 0

    async def communicate(self, data=None):
        return (b"", b"")


async def test_send_command_sequence(monkeypatch):
    s = _sess()
    s._started = True
    s._closed = False
    s._tmux_bin = "tmux"
    s._pane = "%7"

    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(list(args))
        return _FakeProc()

    monkeypatch.setattr(
        "awm.agentcore.claude_backend.asyncio.create_subprocess_exec",
        fake_exec,
    )

    await s.send("multi\nline")

    # load-buffer (text via stdin) → paste-buffer -p (bracketed) → send-keys Enter
    assert calls[0][1] == "load-buffer" and calls[0][2] == "-b"
    paste = calls[1]
    assert paste[1] == "paste-buffer" and "-p" in paste and "-d" in paste
    assert paste[-2:] == ["-t", "%7"]
    last = calls[2]
    assert last[1:] == ["send-keys", "-t", "%7", "Enter"]


async def test_interrupt_sends_escape_then_pastes(monkeypatch):
    """The forced-interrupt seam: ESC to cancel the in-flight turn, THEN the
    same bracketed-paste prompt sequence as send()."""
    s = _sess()
    s._started = True
    s._closed = False
    s._tmux_bin = "tmux"
    s._pane = "%7"

    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(list(args))
        return _FakeProc()

    monkeypatch.setattr(
        "awm.agentcore.claude_backend.asyncio.create_subprocess_exec",
        fake_exec,
    )

    await s.interrupt("urgent")

    # ESC first (cancel current generation) …
    assert calls[0][1:] == ["send-keys", "-t", "%7", "Escape"]
    # … then load-buffer → paste-buffer -p → send-keys Enter.
    assert calls[1][1] == "load-buffer"
    assert calls[2][1] == "paste-buffer" and "-p" in calls[2]
    assert calls[3][1:] == ["send-keys", "-t", "%7", "Enter"]


# ---------------------------------------------------------------------------
# wait()/alive() over a mocked tmux has-session
# ---------------------------------------------------------------------------

class _Run:
    def __init__(self, rc):
        self.returncode = rc


def test_alive_reads_has_session(monkeypatch):
    s = _sess()
    s._tmux_bin = "tmux"
    s._tmux_session = "awm-x"
    s._started = True
    s._closed = False

    monkeypatch.setattr(
        "awm.agentcore.claude_backend.subprocess.run",
        lambda *a, **k: _Run(0),
    )
    assert s.alive() is True
    monkeypatch.setattr(
        "awm.agentcore.claude_backend.subprocess.run",
        lambda *a, **k: _Run(1),
    )
    assert s.alive() is False
    # closed short-circuits without invoking tmux
    s._closed = True
    assert s.alive() is False


async def test_wait_returns_when_session_gone(monkeypatch):
    s = _sess()
    s._tmux_bin = "tmux"
    s._tmux_session = "awm-x"
    s._started = True
    s._closed = False

    state = {"alive": True}
    monkeypatch.setattr(ClaudeSession, "alive", lambda self: state["alive"])

    async def killer():
        await asyncio.sleep(0.1)
        state["alive"] = False

    asyncio.create_task(killer())
    rc = await asyncio.wait_for(s.wait(), timeout=3)
    assert rc is None


async def test_start_writes_settings_and_launcher(monkeypatch, tmp_path):
    """_start writes the settings + launcher files and creates the tmux session
    (tmux + trust acceptance mocked)."""
    s = ClaudeSession(AgentConfig(
        harness="claude", workdir=str(tmp_path), permissions="full",
        tmux_session_name="awm-test",
    ))

    monkeypatch.setattr(
        "awm.agentcore.claude_backend.resolve_bin",
        lambda name: f"/bin/{name}",
    )
    new_session_args = {}

    async def fake_tmux_out(self, *args):
        if args and args[0] == "new-session":
            new_session_args["args"] = args
        return "%3\n"

    async def fake_accept(self):
        return None

    monkeypatch.setattr(ClaudeSession, "_tmux_out", fake_tmux_out)
    monkeypatch.setattr(ClaudeSession, "_accept_trust", fake_accept)

    await s._start()

    assert s._pane == "%3"
    assert s._tmux_session == "awm-test"
    assert s.session_id  # minted a uuid
    # settings + launcher landed in the temp dir
    settings_path = os.path.join(s._tmpdir, "settings.json")
    launch_path = os.path.join(s._tmpdir, "launch.sh")
    assert os.path.exists(settings_path) and os.path.exists(launch_path)
    settings = json.loads(open(settings_path).read())
    assert "SessionStart" in settings["hooks"] and "Stop" in settings["hooks"]
    launcher = open(launch_path).read()
    assert "export AWM_AGENTCORE_CONTROL_FILE=" in launcher
    assert "--dangerously-skip-permissions" in launcher
    # new-session targeted the right name + workdir
    ns = new_session_args["args"]
    assert "awm-test" in ns and str(tmp_path) in ns

    await s.close()
