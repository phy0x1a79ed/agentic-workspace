"""awm.agentcore — a leaf harness layer.

One job: talk to a ``claude`` or ``opencode`` subprocess and yield normalized
:class:`AgentEvent`s, in ``live`` or ``oneshot`` mode, by config. Reused by the
agents service (live chat) and the stt cleanup (one-shot). NO scope /
transcript / gateway concerns; NO ``awm.config`` import — this stays a leaf.

Public surface::

    from awm.agentcore import (
        AgentConfig, AgentEvent, AgentSession, open_agent, run_once,
    )

    cfg = AgentConfig(harness="claude", mode="live", workdir="/path")
    session = open_agent(cfg)
    await session.send("hello")
    async for ev in session.events():
        ...
    await session.close()

    text = await run_once(
        AgentConfig(harness="opencode", mode="oneshot"), "clean this up"
    )
"""

from __future__ import annotations

from .api import open_agent, run_once
from .session import AgentSession
from .types import AgentConfig, AgentEvent, EventKind, Harness, Mode

__all__ = [
    "AgentConfig",
    "AgentEvent",
    "AgentSession",
    "EventKind",
    "Harness",
    "Mode",
    "open_agent",
    "run_once",
]
