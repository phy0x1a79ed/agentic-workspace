"""Unit tests for the interactive tmux terminal session resolver.

The full PTY relay (`terminal_session`) is integration-level — it spawns a real
`tmux attach` against a live agent session and is exercised by the live-hub
harness. Here we cover the pure resolution logic that decides whether a relay
can even start: which agent the init identity names, and whether it is a claude
agent (so it has a tmux pane to attach)."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import uuid

import pytest

from awm.agents import terminal_session as ts
from awm.agents import agent_instances as ai


class _FakeInstance:
    def __init__(self, tmux_session=None):
        self.tmux_session = tmux_session


def test_init_requires_identity():
    sess, err = ts._resolve_tmux_session({})
    assert sess is None
    assert "init requires" in err


def test_invalid_session_id():
    sess, err = ts._resolve_tmux_session({"session_id": "nope"})
    assert sess is None
    assert "invalid session_id" in err


def test_no_live_session(monkeypatch):
    monkeypatch.setattr(ai, "get_session_by_scope", lambda s: None)
    sess, err = ts._resolve_tmux_session({"scope": "s"})
    assert sess is None
    assert "no live agent session" in err


def test_non_tmux_agent_has_no_terminal(monkeypatch):
    # An opencode agent (the only non-tmux harness now): no tmux_session →
    # nothing to attach.
    monkeypatch.setattr(ai, "get_session_by_scope",
                        lambda s: _FakeInstance(tmux_session=None))
    sess, err = ts._resolve_tmux_session({"scope": "s"})
    assert sess is None
    assert "no tmux session" in err


def test_resolves_tmux_session_by_scope(monkeypatch):
    monkeypatch.setattr(ai, "get_session_by_scope",
                        lambda s: _FakeInstance(tmux_session="awm-7-s"))
    sess, err = ts._resolve_tmux_session({"scope": "s"})
    assert err is None
    assert sess == "awm-7-s"


def test_resolves_tmux_session_by_session_id(monkeypatch):
    monkeypatch.setattr(ai, "get_session",
                        lambda sid: _FakeInstance(tmux_session="awm-9-x"))
    sess, err = ts._resolve_tmux_session({"session_id": "9"})
    assert err is None
    assert sess == "awm-9-x"


def test_resolves_adhoc_tmux_by_name(monkeypatch):
    # An explicit tmux_session (machine-wide roster row, not a registry agent):
    # verified to exist, returned directly — registry-agnostic.
    monkeypatch.setattr(ts, "_tmux_has_session", lambda n: True)
    sess, err = ts._resolve_tmux_session({"tmux_session": "fleet-proj-ab12"})
    assert err is None
    assert sess == "fleet-proj-ab12"


def test_adhoc_tmux_missing_is_error(monkeypatch):
    monkeypatch.setattr(ts, "_tmux_has_session", lambda n: False)
    sess, err = ts._resolve_tmux_session({"tmux_session": "gone-xyz"})
    assert sess is None
    assert "no tmux session named" in err


def test_adhoc_tmux_wins_over_scope(monkeypatch):
    # If both an explicit name and a scope are present, the ad-hoc name is used
    # (and the registry is never consulted).
    monkeypatch.setattr(ts, "_tmux_has_session", lambda n: True)
    monkeypatch.setattr(ai, "get_session_by_scope",
                        lambda s: (_ for _ in ()).throw(AssertionError("used registry")))
    sess, err = ts._resolve_tmux_session(
        {"tmux_session": "adhoc-1", "scope": "s"})
    assert err is None and sess == "adhoc-1"


def test_terminal_registered_in_manifest():
    from awm.agents.hub_adapter import API_MANIFEST
    kinds = {s["kind"] for s in API_MANIFEST["sessions"]}
    assert "terminal" in kinds


def test_spawn_and_kill_tmux_in_manifest():
    from awm.agents.hub_adapter import API_MANIFEST, HANDLERS
    names = {f["name"] for f in API_MANIFEST["functions"]}
    assert {"spawn", "kill_tmux"} <= names
    assert "spawn" in HANDLERS and "kill_tmux" in HANDLERS
    # UI/human action — deliberately off the MCP surface.
    spawn = next(f for f in API_MANIFEST["functions"] if f["name"] == "spawn")
    assert "mcp" not in spawn["surfaces"]


# ---------------------------------------------------------------------------
# Live PTY relay against a real tmux session (the core T3 mechanism)
# ---------------------------------------------------------------------------

class _FakeBridge:
    """In-memory stand-in for the hub bridge: captures server→client frames in
    `outgoing` and feeds client→server frames from `incoming`."""

    def __init__(self):
        self.outgoing: list = []
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send(self, data):
        self.outgoing.append(data)

    async def recv(self):
        if self.closed:
            raise ConnectionError("bridge closed")
        return await self.incoming.get()

    async def close(self):
        self.closed = True


class _FakeCtx:
    """Duck-typed SessionContext: only `.init` and `.open_bridge()` are used."""

    def __init__(self, init, bridge):
        self.init = init
        self._bridge = bridge

    async def open_bridge(self):
        return self._bridge


class _FakeTmuxInstance:
    def __init__(self, tmux_session):
        self.tmux_session = tmux_session


@pytest.mark.slow
@pytest.mark.asyncio
async def test_pty_relay_roundtrip_against_real_tmux(monkeypatch):
    """End-to-end PTY relay: attach to a REAL tmux session running `cat`, type a
    line, and see it echoed back through the byte relay. Exercises the parts that
    can only fail live — tmux needs a controlling tty, the PTY fd plumbing, and
    bidirectional add_reader/os.write wiring."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux not available")

    sname = f"awm-ittest-{uuid.uuid4().hex[:8]}"
    # A pane running `cat`: keystrokes are echoed (pane line-discipline + cat),
    # so the relay's output stream should contain what we typed.
    subprocess.run(["tmux", "new-session", "-d", "-s", sname, "cat"], check=True)
    try:
        monkeypatch.setattr(ai, "get_session_by_scope",
                            lambda s: _FakeTmuxInstance(sname))
        bridge = _FakeBridge()
        ctx = _FakeCtx({"scope": "s", "cols": 80, "rows": 24},
                       bridge)
        task = asyncio.create_task(ts.terminal_session(ctx))

        # Let the attach + initial paint settle, then type a line.
        await asyncio.sleep(0.6)
        await bridge.incoming.put(b"ping123\r")

        # Poll the relayed output for the echo (timing-tolerant).
        seen = b""
        for _ in range(40):  # up to ~4s
            await asyncio.sleep(0.1)
            seen = b"".join(d for d in bridge.outgoing
                            if isinstance(d, (bytes, bytearray)))
            if b"ping123" in seen:
                break
        assert b"ping123" in seen, f"echo not relayed; got {seen[:200]!r}"

        # A resize control frame must not break the relay.
        await bridge.incoming.put(json.dumps({"type": "resize", "cols": 100, "rows": 30}))
        await asyncio.sleep(0.2)

        # Closing the bridge tears the handler down (detaches; does not hang).
        await bridge.close()
        await bridge.incoming.put(b"")  # unblock a pending recv()
        await asyncio.wait_for(task, timeout=5)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", sname],
                       capture_output=True)

    # The agent's session is detached, not killed — but this test owns the
    # session, so it's already gone via kill-session above. Assert teardown ran.
    assert bridge.closed is True
