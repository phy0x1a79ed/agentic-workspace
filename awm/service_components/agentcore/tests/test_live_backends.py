"""Live backend tests — spawn the real claude/opencode CLI.

Skipped automatically when the CLI isn't installed. Marked ``live`` (slow,
network). Each harness gets a one-shot test and a short multi-turn live test.

Run only these:
    PYTEST_ARGS="-m live" awm/gateway/scripts/run-tests.sh agentcore
"""

from __future__ import annotations

import pytest

from awm.agentcore import AgentConfig, open_agent, run_once
from .conftest import requires_claude, requires_opencode

pytestmark = pytest.mark.live


async def _drain_first_message(session, timeout=120.0) -> str:
    """Send nothing more; collect message text until the terminal event."""
    import asyncio
    texts: list[str] = []

    async def _collect():
        async for ev in session.events():
            if ev.kind == "message" and ev.text:
                texts.append(ev.text)
            elif ev.kind in ("result", "error"):
                return
    await asyncio.wait_for(_collect(), timeout=timeout)
    return "\n".join(texts)


# ---------------------------------------------------------------------------
# claude
# ---------------------------------------------------------------------------

@requires_claude
async def test_claude_oneshot():
    cfg = AgentConfig(harness="claude", mode="oneshot")
    out = await run_once(cfg, "Reply with exactly the word PONG and nothing else.")
    assert isinstance(out, str)
    assert "PONG" in out.upper()


@requires_claude
async def test_claude_live_multiturn():
    cfg = AgentConfig(harness="claude", mode="live")
    session = open_agent(cfg)
    try:
        await session.start()
        await session.send("Remember the number 42. Reply OK.")
        first = await _drain_first_message(session)
        assert first  # got some reply
        await session.send("What number did I ask you to remember? Reply with just the number.")
        second = await _drain_first_message(session)
        assert "42" in second
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# opencode
# ---------------------------------------------------------------------------

@requires_opencode
async def test_opencode_oneshot():
    cfg = AgentConfig(harness="opencode", mode="oneshot")
    out = await run_once(cfg, "Reply with exactly the word PONG and nothing else.")
    assert isinstance(out, str)
    assert "PONG" in out.upper()


@requires_opencode
async def test_opencode_live_multiturn():
    cfg = AgentConfig(harness="opencode", mode="live")
    session = open_agent(cfg)
    try:
        await session.start()
        await session.send("Remember the number 42. Reply OK.")
        first = await _drain_first_message(session)
        assert first
        await session.send("What number did I ask you to remember? Reply with just the number.")
        second = await _drain_first_message(session)
        assert "42" in second
    finally:
        await session.close()


@requires_opencode
async def test_opencode_oneshot_schema():
    cfg = AgentConfig(harness="opencode", mode="oneshot")
    out = await run_once(
        cfg,
        "Return a JSON object with a 'greeting' key set to the string 'hi'.",
        schema={
            "type": "object",
            "properties": {"greeting": {"type": "string"}},
            "required": ["greeting"],
        },
    )
    assert isinstance(out, dict)
    assert "greeting" in out
