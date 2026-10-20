"""What a keeper does this tick about one kept session."""

from __future__ import annotations

import enum
from dataclasses import dataclass

from awm.kept.breaker import Breaker

#: Resumes that did not reach healthy before the keeper starts fresh instead.
MAX_RESUMES = 3


class Action(str, enum.Enum):
    NONE = "none"
    WAIT = "wait"
    BACKOFF = "backoff"
    RESUME = "resume"
    FRESH = "fresh"
    GIVE_UP = "give_up"


@dataclass(frozen=True)
class Situation:
    """What the keeper knows about the session this tick.

    ``state`` is ``alive``, ``blocked`` (a person must act), ``failed`` (an
    error only a new session mends), ``exited`` or ``missing``. ``daemon_up``
    is None when the node did not say.
    """

    state: str
    node_reachable: bool = True
    daemon_up: bool | None = True
    resumable: bool = True
    spec_changed: bool = False


def decide(s: Situation, breaker: Breaker, now: float) -> tuple[Action, str]:
    """The action and a short reason for it.

    A session on a node that cannot run it is waited for, never stopped or
    replaced: replacing it there would discard a conversation that a resume
    keeps once the daemon is back.
    """
    if not s.node_reachable:
        return Action.WAIT, "its node does not answer"
    if s.daemon_up is False:
        return Action.WAIT, "no Claude Code daemon runs on its node"
    if s.state in ("alive", "blocked"):
        return Action.NONE, ""
    if breaker.tripped(now):
        return Action.GIVE_UP, breaker.reason
    if now < breaker.next_try:
        return Action.BACKOFF, f"next try in {breaker.next_try - now:.0f}s"
    if s.state == "missing":
        return Action.FRESH, "it has no session"
    if s.state == "failed":
        return Action.FRESH, "it failed on an error a resume cannot mend"
    if s.spec_changed:
        return Action.FRESH, "its launch spec changed"
    if not s.resumable:
        return Action.FRESH, "it cannot be resumed"
    if breaker.recent(now, "resume") >= MAX_RESUMES:
        return Action.FRESH, f"{MAX_RESUMES} resumes did not stay up"
    return Action.RESUME, "it exited"
