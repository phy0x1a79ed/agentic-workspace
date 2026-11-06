"""Who holds a pushed channel, a social seat or a channel room, and how to tell they are gone (channels.md R69).

The holder is never named by the model. A session is known by its R9 id
(``sessions.md`` R9). The id comes from ``_caller_session_id`` when the gateway
sends one: the Rust gateway does, and the isolated door does for a container
session, which has no host pid. Otherwise the gateway's ``_caller_pid`` names a
session on this node, read from its record. A call from another node arrives
through that node's edge, which stamps the verified ``peer:<node>`` and carries
``_caller_session``, the claim that node read from its own record.

A line is typed into the holder through ``reflection message``, so a holder is a
background session, named by its job, or an isolated session, named by its R9
id (``channels.md`` R111). It lives here because a plugin may not import another
plugin's code (``install.md`` R117 item 1).
"""

from __future__ import annotations

import logging

from awm import config, gatewayclient
from awm.gatewayclient import ids

log = logging.getLogger("awm.gatewayclient.holders")

_DEAD_STATES = ("gone", "stopped")

SESSION_KEY = "_caller_session_id"
PID_KEY = "_caller_pid"
CLAIM_KEY = "_caller_session"

ISOLATED = "isolated"


class HolderError(ValueError):
    """The calling session cannot hold a channel. The message says why."""


def claim(pid: int) -> dict | None:
    """What a borrower forwards as ``_caller_session``: the local session ``pid``, or None."""
    from awm.gatewayclient import sessions

    rec = sessions.session_of(pid)
    if not rec:
        return None
    return {"kind": "bg" if rec.get("hosting") == "background" else "interactive",
            "job": str(rec.get("job") or ""), "name": str(rec.get("name") or ""),
            "session_id": rec.get("session_id")}


def _session_rows(include_stopped: bool = False) -> list[dict] | None:
    """Every session on this node, from ``reflection list_other``, or None.

    None when reflection cannot answer, when a harness plugin errored, or when
    the rows carry no R9 ``session_id``: "could not tell" closes nothing.
    """
    args = {"include_stopped": True} if include_stopped else {}
    try:
        out = gatewayclient.call_sync("reflection", "list_other", args, timeout=30.0)
    except Exception as exc:  # noqa: BLE001 — "could not tell" closes nothing
        log.info("reflection list_other failed: %s", exc)
        return None
    if not isinstance(out, dict) or out.get("ok") is False or out.get("errors"):
        return None
    rows = [r for r in out.get("sessions") or [] if isinstance(r, dict)]
    if not rows or not all(r.get("session_id") for r in rows):
        return None
    return rows


def row_live(row: dict) -> bool:
    """Whether a ``reflection list_other`` row is a running session."""
    return (row.get("state") not in _DEAD_STATES and row.get("live") is not False
            and row.get("alive") is not False)


def _push_target(session: str) -> str | None:
    """What ``reflection message`` types into for ``session`` on this node: its job,
    its own R9 id when it is isolated, or None when nothing can be typed into it."""
    rows = _session_rows(include_stopped=True)
    for row in rows or []:
        if str(row["session_id"]).lower() != session:
            continue
        if row.get("job"):
            return str(row["job"])
        if row.get("kind") == ISOLATED:
            return session
    return None


def _from_claim(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    # A gateway's door claim names no kind: it carries a job only for a background session.
    door = "kind" not in raw and "hosting" not in raw and bool(raw.get("node"))
    return {"background": raw.get("kind") == "bg" or raw.get("hosting") == "background"
                          or (door and bool(raw.get("job"))),
            "job": str(raw.get("job") or ""),
            "session": raw.get("session_id") or ids.to_r9(raw.get("sessionId")),
            "name": str(raw.get("name") or "")}


def caller(args: dict, as_: str | None, *, what: str = "a channel") -> dict:
    """The calling session as ``{node, session, job, name}``, or a :class:`HolderError`.

    ``job`` is what a push types into. ``session`` is the R9 id when one is
    known, else ``job:<job>``. ``pid`` joins them for a session on this node.
    ``what`` names the held thing in the refusal.
    """
    from awm.config import edges
    from awm.gatewayclient import sessions

    pid = args.get(PID_KEY)
    has_pid = isinstance(pid, int) and not isinstance(pid, bool)
    sid = ids.to_r9(args.get(SESSION_KEY))
    if has_pid:
        rec = sessions.session_of(pid)
        found = None if not rec else {
            "background": rec.get("hosting") == "background",
            "job": str(rec.get("job") or ""), "session": rec.get("session_id"),
            "name": str(rec.get("name") or "")}
        node = config.node_name()
    elif sid:
        target = _push_target(sid)
        found = {"background": bool(target), "job": target or "", "session": sid, "name": ""}
        node = config.node_name()
    else:
        found, node = _from_claim(args.get(CLAIM_KEY)), edges.caller_peer(as_)
    if not node or found is None:
        raise HolderError(f"no calling session reached {what}: it is held by a "
                          "running agent session, so call this from one")
    if not found["background"] or not found["job"]:
        raise HolderError(f"only a background session, or an isolated one, can hold {what}: "
                          "each message is typed into the holder's prompt")
    session = sid or found.get("session") or f"job:{found['job']}"
    holder = {"node": node, "session": str(session), "job": found["job"],
              "name": found["name"]}
    if has_pid:
        holder["pid"] = pid
    return holder


def live_sessions() -> set[str] | None:
    """The R9 ids of the sessions running on this node, or None when it cannot tell.

    Blocking: call it off the event loop.
    """
    rows = _session_rows()
    if rows is None:
        return None
    return {str(r["session_id"]).lower() for r in rows if row_live(r)}
