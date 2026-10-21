"""Who is calling, so that a session answers only to the operator who minted it.

One daemon serves every agent on a node, and two agents helping the same owner
are still doing different work. So a session belongs to the caller that minted
it, and the verbs default to the caller's own sessions.

The caller is never named by the model. The gateway stamps ``_caller_pid``, the
pid of the Claude Code session behind the call, and a call from another node
carries ``_caller_session``, the claim that node read from its own session
record. The identity kept is the most durable one that record offers: a
background session's job id survives its restarts and compactions, while an
interactive session is only as durable as its process.

This separates operators who share a Unix user; it is not a wall between them.
Each of them can read the session files and subscribe to the raw topic. What it
stops is one agent reading or driving another's session by default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PID_KEY = "_caller_pid"
CLAIM_KEY = "_caller_session"


@dataclass(frozen=True)
class Caller:
    #: None when no session stands behind the call: a person at a terminal, or
    #: a gateway too old to say.
    id: str | None
    label: str

    def owns(self, owner: dict | None) -> bool:
        return owner is not None and owner.get("by") == self.id


TERMINAL = Caller(None, "a terminal on this host")


def _record(pid: int) -> dict | None:
    try:
        from awm.claudedaemon import roster
    except ImportError:
        return None
    return roster.session_by_pid(pid)


def _node_of(as_: str) -> str:
    try:
        from awm.config import peerbook
        return peerbook.caller_peer(as_) or as_
    except Exception:  # noqa: BLE001 — a label, never a decision
        return as_


def of(args: dict, as_: str | None) -> Caller:
    """The caller of one verb, from what the gateway stamped on it."""
    pid = args.get(PID_KEY)
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        rec = _record(pid) or {}
        name = str(rec.get("name") or "")
        if rec.get("kind") == "bg" and rec.get("jobId"):
            job = str(rec["jobId"])
            return Caller(f"job:{job}", f"{name} (job {job})" if name else f"job {job}")
        started = str(rec.get("procStart") or "")
        return Caller(f"pid:{pid}:{started}", f"{name} (pid {pid})" if name else f"pid {pid}")
    claim = args.get(CLAIM_KEY)
    if as_:
        node = _node_of(as_)
        if isinstance(claim, dict) and claim.get("job"):
            job = str(claim["job"])
            return Caller(f"{node}/job:{job}", f"job {job} on {node}")
        return Caller(f"{node}/-", f"a caller on {node}")
    return TERMINAL


def strip(args: dict) -> dict:
    """``args`` without the identity the gateway stamped, for the daemon."""
    return {k: v for k, v in args.items() if k not in (PID_KEY, CLAIM_KEY)}


def truthy(value: Any) -> bool:
    return value in (True, "true", "True", "1", 1)
