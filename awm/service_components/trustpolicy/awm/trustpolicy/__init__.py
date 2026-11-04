"""Trust levels per session (fleet.md R103, R104, R107, channels.md R70).

A pure policy library with no state. reflection keeps each session's level in
its ledger and asks this library before it starts, steers or messages a
session. The journal asks it for a caller's read and write bounds.

The level comes only from what started the session. A starter sets a level
equal to its own or lower. A raise above the starter's level takes a
``trust_raise`` grant that Tony approved, naming that session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from verify_grant import GrantInvalid as GrantError, canon as canonical, payload_digest, verify_grant

LEVELS = ("untrusted", "cooperative", "trusted")

#: Verbs that act on another session. ``message`` is apart: it carries words,
#: never authority, so it may go to a higher level (a seat asks the main seat).
STEER_VERBS = frozenset({
    "start_other", "resume_other", "stop_other", "stop", "interrupt", "model",
    "effort", "rename", "cd", "compact", "mode", "remote_control",
})

FIRST_LOCAL_MESSAGE_HINT = (
    "This session runs on your own node. Next time, reach it with your harness's "
    "own messaging tool (SendMessage in Claude Code). awm carries only messages "
    "that cross an edge to another node.")


class TrustError(PermissionError):
    """A start or a steer above what the caller's level allows."""


def rank(level: str) -> int:
    try:
        return LEVELS.index(level)
    except ValueError:
        raise ValueError(f"trust level {level!r}: expected one of {LEVELS}") from None


def lower(a: str, b: str) -> str:
    return a if rank(a) <= rank(b) else b


def level_for_start(starter_level: str, requested: str | None = None, *,
                    session_id: str | None = None, grant: dict | None = None,
                    grant_key: bytes | None = None, node: str | None = None,
                    now: float | None = None) -> str:
    """The level a new session starts at.

    ``requested`` defaults to the starter's level. A level above the starter's
    needs ``grant``: a ``trust_raise`` grant whose subject is ``session_id``
    and whose payload is ``{session_id, from, to}``, checked with
    ``grant_key`` (alcor's raw Ed25519 public key).
    """
    rank(starter_level)
    want = requested or starter_level
    if rank(want) <= rank(starter_level):
        return want
    raise_grant(session_id, starter_level, want, grant=grant, grant_key=grant_key,
                node=node, now=now)
    return want


def raise_grant(session_id: str | None, current: str, to: str, *, grant: dict | None,
                grant_key: bytes | None, node: str | None = None,
                now: float | None = None) -> dict:
    """The verified ``trust_raise`` grant for ``session_id``, or :class:`TrustError`."""
    if not session_id or grant is None or grant_key is None:
        raise TrustError(f"a raise from {current} to {to} takes a grant Tony approves")
    payload = {"session_id": session_id, "from": current, "to": to}
    try:
        return verify_grant(grant, Ed25519PublicKey.from_public_bytes(grant_key), kind="trust_raise",
                            node=node, subject=session_id, payload=payload, now=now)
    except GrantError as exc:
        raise TrustError(f"raise to {to} refused: {exc}") from exc


def may_steer(actor_level: str, verb: str, *, target_level: str | None = None) -> tuple[bool, str]:
    """Whether a session at ``actor_level`` may run ``verb`` on another session.

    An untrusted session starts, steers and messages no session (R104 item 3,
    R109 item 2). Others may message any session. A steer needs a level at
    least the target's, so no session steers one above it.
    """
    rank(actor_level)
    if actor_level == "untrusted":
        return False, "an untrusted session cannot start, steer or message other sessions"
    if verb == "message" or verb not in STEER_VERBS:
        return True, ""
    if target_level is not None and rank(target_level) > rank(actor_level):
        return False, f"a {actor_level} session cannot {verb} a {target_level} session"
    return True, ""


def cross_edge(starter_node: str, target_node: str, starter_level: str,
               reaches: Callable[[str, str], bool] | None = None) -> tuple[bool, str]:
    """Whether a session on ``starter_node`` may act on ``target_node`` (R104 item 1).

    Returns ``(True, level)`` with the level a session it starts there takes,
    which is the starter's own (R104 item 3), or ``(False, reason)``.
    """
    if reaches is None:
        from awm.config.edges import reaches
    if starter_node != target_node and not reaches(starter_node, target_node):
        return False, f"{starter_node} has no edge to {target_node}"
    ok, why = may_steer(starter_level, "start_other")
    return (True, starter_level) if ok else (False, why)


@dataclass(frozen=True)
class JournalBound:
    """What a session may read and write in the journals (R107 item 3).

    ``read_nodes`` is ``None`` for no node bound. ``read_scope`` is the one
    scope it reads, or ``None`` for every scope on its readable nodes.
    ``write_scope`` is the one scope it writes, or ``None`` for any.
    """

    read_nodes: frozenset[str] | None
    read_scope: str | None
    write_scope: str | None

    def may_read(self, node: str, project: str | None, scope: str | None) -> bool:
        if self.read_nodes is not None and node not in self.read_nodes:
            return False
        return self.read_scope is None or _scope_key(project, scope) == self.read_scope

    def may_write(self, project: str | None, scope: str | None) -> bool:
        return self.write_scope is None or _scope_key(project, scope) == self.write_scope


def _scope_key(project: str | None, scope: str | None) -> str:
    return f"{project or ''}/{scope or ''}"


def journal_bound(level: str, *, node: str, project: str | None, scope: str | None,
                  reachable: Callable[[str], list[str]] | None = None) -> JournalBound:
    """The journal bound of a session at ``level`` in ``project/scope`` on ``node``.

    - trusted reads and writes its node's journals and every node its node has an edge to.
    - cooperative reads its node's journals and writes only its own scope.
    - untrusted reads and writes only its own scope.
    """
    rank(level)
    own = _scope_key(project, scope)
    if level == "trusted":
        if reachable is None:
            from awm.config.edges import reachable_from as reachable
        return JournalBound(frozenset({node, *reachable(node)}), None, None)
    if level == "cooperative":
        return JournalBound(frozenset({node}), None, own)
    return JournalBound(frozenset({node}), own, own)


__all__ = [
    "LEVELS", "STEER_VERBS", "FIRST_LOCAL_MESSAGE_HINT", "TrustError", "GrantError",
    "JournalBound", "rank", "lower", "level_for_start", "raise_grant", "may_steer",
    "cross_edge", "journal_bound", "verify_grant", "canonical", "payload_digest",
]
