"""Who holds a channel, how a line reaches them, and how to tell they are gone.

The holder is never named by the model. A session is known by its R9 id
(``sessions.md`` R9), and a channel belongs to that session: it closes when the
session ends. The id comes from ``_caller_session_id`` when the gateway sends
one, as the Rust gateway does. Otherwise the gateway's ``_caller_pid`` names a
session on this node, read from its record. A call from another node arrives
through that node's edge, which stamps the verified ``peer:<node>`` and carries
``_caller_session``, the claim that node read from its own record.

A line is typed into the holder's job, so only a background session can hold a
channel. The push and the dead-holder check are ``awm.gatewayclient.push`` (R69).
"""

from __future__ import annotations

import logging

from awm import config, gatewayclient
from awm.gatewayclient import ids

log = logging.getLogger("awm.channel.holders")

_DEAD_STATES = ("gone", "stopped")

SESSION_KEY = "_caller_session_id"
PID_KEY = "_caller_pid"
CLAIM_KEY = "_caller_session"


class ChannelError(ValueError):
    """A channel verb refused. The message says why and what to do instead."""


def _record(pid: int) -> dict | None:
    """``{background, job, session, name}`` for the session behind ``pid``, from reflection."""
    from awm.gatewayclient import sessions

    rec = sessions.session_of(pid)
    if not rec:
        return None
    return {"background": rec.get("hosting") == "background",
            "job": str(rec.get("job") or ""), "session": rec.get("session_id"),
            "name": str(rec.get("name") or "")}


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


def _row_live(row: dict) -> bool:
    return (row.get("state") not in _DEAD_STATES and row.get("live") is not False
            and row.get("alive") is not False)


def _job_of_session(session: str) -> str | None:
    """The background job on this node whose session is ``session``."""
    rows = _session_rows(include_stopped=True)
    if rows is not None:
        for row in rows:
            if str(row["session_id"]).lower() == session and row.get("job"):
                return str(row["job"])
    return None


def caller(args: dict, as_: str | None) -> dict:
    """The calling session as ``{node, session, job, name}``, or a :class:`ChannelError`.

    ``session`` is the R9 id when one is known, else ``job:<job>``. ``pid`` joins
    them for a session on this node.
    """
    from awm.config import edges

    pid = args.get(PID_KEY)
    has_pid = isinstance(pid, int) and not isinstance(pid, bool)
    sid = args.get(SESSION_KEY)
    sid = sid.strip() if isinstance(sid, str) and sid.strip() else None
    if has_pid:
        claim, node = _record(pid), config.node_name()
    elif sid:
        job = _job_of_session(sid)
        claim = {"background": bool(job), "job": job or "", "session": sid, "name": ""}
        node = config.node_name()
    else:
        raw, node = args.get(CLAIM_KEY), edges.caller_peer(as_)
        claim = None
        if isinstance(raw, dict):
            claim = {"background": raw.get("kind") == "bg" or raw.get("hosting") == "background",
                     "job": str(raw.get("job") or ""),
                     "session": raw.get("session_id") or ids.to_r9(raw.get("sessionId")),
                     "name": str(raw.get("name") or "")}
    if not node or not isinstance(claim, dict):
        raise ChannelError("no calling session reached channel: a channel is held "
                           "by a running agent session, so call this from one")
    if not claim["background"] or not claim["job"]:
        raise ChannelError("only a background session can hold a channel: each "
                           "message is typed into the holder's job")
    session = sid or claim.get("session") or f"job:{claim['job']}"
    holder = {"node": node, "session": str(session), "job": claim["job"],
              "name": claim["name"]}
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
    return {str(r["session_id"]).lower() for r in rows if _row_live(r)}
