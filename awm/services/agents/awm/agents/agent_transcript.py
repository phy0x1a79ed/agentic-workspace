"""Per-agent structured transcript — writes to agents.db agent_transcript table.

Modular change from the monolith: transcript writes go to the agents service's
own DB (agent_transcript table) rather than being forwarded to a separate
transcript store. The compact subscriber and assistant-turn notifier remain
in-memory only.
"""
from __future__ import annotations

import asyncio
import json

from awm.agents.dao import AgentsDAO
from awm.agents._time import now_ms


def _get_dao() -> AgentsDAO:
    return AgentsDAO()


# ---------------------------------------------------------------------------
# Event-type classification (unchanged)
# ---------------------------------------------------------------------------

def _classify(parsed: dict) -> str:
    t = parsed.get("type")
    if t == "system" and parsed.get("subtype") == "init":
        return "session_start"
    if t == "assistant":
        if parsed.get("partial") or parsed.get("subtype") == "partial":
            return "partial"
        return "message"
    if t == "user":
        content = parsed.get("message", {}).get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return "tool_result"
        return "message"
    if t == "result":
        return "system"
    if t == "tool_use":
        return "tool_use"
    return "partial"


def _is_end_of_turn_assistant(parsed: dict) -> bool:
    if parsed.get("type") != "assistant":
        return False
    if parsed.get("partial") or parsed.get("subtype") == "partial":
        return False
    msg = parsed.get("message", {})
    stop_reason = msg.get("stop_reason")
    return stop_reason in ("end_turn", "stop_sequence", "tool_use")


def _extract_assistant_text(parsed: dict) -> str:
    content = parsed.get("message", {}).get("content", [])
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            t = block.get("text", "")
            if isinstance(t, str) and t.strip():
                parts.append(t)
    return "\n".join(parts).strip()


# ---------------------------------------------------------------------------
# End-of-turn notifier
# ---------------------------------------------------------------------------

_assistant_turn_subs: dict[int, set[asyncio.Queue]] = {}


def subscribe_assistant_turns(handle_id: int) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=8)
    _assistant_turn_subs.setdefault(handle_id, set()).add(q)
    return q


def unsubscribe_assistant_turns(handle_id: int, queue: asyncio.Queue) -> None:
    bucket = _assistant_turn_subs.get(handle_id)
    if bucket is None:
        return
    bucket.discard(queue)
    if not bucket:
        _assistant_turn_subs.pop(handle_id, None)


def _broadcast_assistant_turn(handle_id: int, text: str) -> None:
    bucket = _assistant_turn_subs.get(handle_id)
    if not bucket:
        return
    for q in list(bucket):
        try:
            q.put_nowait(text)
        except asyncio.QueueFull:
            pass


# ---------------------------------------------------------------------------
# Write paths
# ---------------------------------------------------------------------------

def record_out(session, parsed: dict) -> None:
    """Persist one parsed stream-json event from the CLI's stdout."""
    kind = _classify(parsed)
    try:
        body_json = json.dumps(parsed)
    except (TypeError, ValueError):
        body_json = json.dumps({"raw": repr(parsed)})
    text_body = _extract_assistant_text(parsed) if parsed.get("type") == "assistant" else ""
    if not text_body:
        text_body = (parsed.get("result") if parsed.get("type") == "result"
                     and isinstance(parsed.get("result"), str) else "") or ""
    meta = {"event": parsed, "_event_repr": body_json}
    try:
        _get_dao().insert_transcript(
            scope=session.scope,
            kind=kind, body=text_body, meta=meta, ts=now_ms(),
        )
    except Exception:  # noqa: BLE001
        pass
    if _is_end_of_turn_assistant(parsed):
        _broadcast_assistant_turn(session.id, _extract_assistant_text(parsed))


def _act_from_row(row: dict) -> dict:
    """Shape one transcript row into the live/backfill wire act.

    ``{id, kind, body, meta, ts}`` — ``id`` is the ``agent_transcript`` uuid
    (the de-dupe key the browser matches the live overlap against)."""
    try:
        meta = json.loads(row.get("meta") or "{}")
    except (TypeError, ValueError):
        meta = {}
    return {
        "id": row["id"],
        "kind": row["kind"],
        "body": row.get("body") or "",
        "meta": meta,
        "ts": row["ts"],
    }


def record_event(session, event) -> dict | None:
    """Persist one agentcore :class:`AgentEvent` and return its wire act.

    This is the agentcore-era write path (``record_out`` persisted raw
    stream-json; this persists the already-normalized event). The event's
    ``kind`` is stored verbatim (message / partial / tool_use / tool_result /
    status / result / error); ``body`` is the human-renderable text; ``meta``
    carries the structured payload plus the agentcore event id and ts. Returns
    the wire act (``{id, kind, body, meta, ts}``) the bus publishes, or None on
    a persistence failure.
    """
    ts = now_ms()
    meta = {
        "event_id": getattr(event, "id", None),
        "event_ts": getattr(event, "ts", None),
        "data": getattr(event, "data", None),
    }
    body = getattr(event, "text", None) or ""
    kind = getattr(event, "kind", "status")
    try:
        row_id = _get_dao().insert_transcript(
            scope=session.scope,
            kind=kind, body=body, meta=meta, ts=ts,
        )
    except Exception:  # noqa: BLE001
        return None
    if kind == "message" and body:
        _broadcast_assistant_turn(session.id, body)
    return {"id": row_id, "kind": kind, "body": body, "meta": meta, "ts": ts}


def upsert_message_act(session, message_id: str, body: str,
                       data: dict | None, ts: int) -> dict | None:
    """Upsert the one durable row for a streamed assistant message.

    The agent's streamed reply is persisted as a SINGLE row keyed by its
    harness ``message_id`` (not one row per partial): the partials stream
    live-only over the bus, and the finalized text is upserted here. ``meta``
    carries ``message_id`` at top level so the frontend's ``coalesceKey`` folds
    the live partial bubble and this finalized row into one chat row. Returns
    the wire act (``{id, kind, body, meta, ts}``) the bus publishes, or None on
    a persistence failure.
    """
    meta = {"message_id": message_id, "data": data}
    try:
        _get_dao().upsert_transcript(
            id=message_id, scope=session.scope,
            kind="message", body=body, meta=meta, ts=ts,
        )
    except Exception:  # noqa: BLE001
        return None
    if body:
        _broadcast_assistant_turn(session.id, body)
    return {"id": message_id, "kind": "message", "body": body,
            "meta": meta, "ts": ts}


def record_raw_out(session, line: str) -> None:
    """Persist a non-JSON stdout line as kind='system'."""
    try:
        _get_dao().insert_transcript(
            scope=session.scope,
            kind="system", body=line, meta={"raw": True}, ts=now_ms(),
        )
    except Exception:  # noqa: BLE001
        pass


def record_in(session, body: str, *, injection: bool = False,
              act_id: str | None = None) -> dict | None:
    """Persist a framed stdin write and fan it out to live subscribers.

    The human's own input is recorded back into the SAME transcript so the chat
    renders the human turn from the stream (no optimistic echo on the wire).
    Like every agent-OUTPUT path (``record_event`` / ``upsert_message_act``),
    this must ALSO publish to the live act bus — otherwise a connected chat only
    learns of the human turn on a reconnect/backfill, which never happens in a
    normal session (the original bug).

    ``act_id`` is an optional client correlation id: when given the row is
    UPSERTED under it, so an optimistic ``sending`` chip the browser folded
    under the same id reconciles in place (one row, never two); when absent a
    server uuid is minted. ``meta.status`` is stamped ``"delivered"`` (the stdin
    write is confirmed by the time we get here). Returns the wire act
    (``{id, kind, body, meta, ts}``) the bus published, or None on a persistence
    failure.
    """
    kind = "slash" if injection else "message"
    ts = now_ms()
    meta = {"direction": "in", "injection": injection, "status": "delivered"}
    try:
        if act_id:
            row_id = _get_dao().upsert_transcript(
                id=act_id, scope=session.scope,
                kind=kind, body=body, meta=meta, ts=ts,
            )
        else:
            row_id = _get_dao().insert_transcript(
                scope=session.scope,
                kind=kind, body=body, meta=meta, ts=ts,
            )
    except Exception:  # noqa: BLE001
        return None
    act = {"id": row_id, "kind": kind, "body": body, "meta": meta, "ts": ts}
    # Lazy import: agent_bus imports only asyncio, so no import cycle.
    try:
        from awm.agents import agent_bus
        agent_bus.publish_act(session.scope, act)
    except Exception:  # noqa: BLE001
        pass
    return act


WORKING_ACT_PREFIX = "working:"


def publish_inbound(session, framed_body: str, act_id: str) -> None:
    """Publish a human turn to the live bus IMMEDIATELY (bus-only, not persisted).

    The responsiveness fix: today the human turn only reaches a connected chat
    inside ``_input_pump`` → :func:`record_in`, which is turn-aligned (it lands
    when the agent next takes a turn). Publishing here, at ``enqueue`` time, makes
    the human turn appear at once — ``record_in`` later upserts the SAME ``act_id``
    so the two fold to one durable row (the browser's optimistic chip reconciles
    by id). ``meta.status = "delivered"`` flips the optimistic ``sending`` chip to
    received promptly."""
    if not act_id:
        return
    act = {"id": act_id, "kind": "message", "body": framed_body,
           "meta": {"direction": "in", "injection": False, "status": "delivered"},
           "ts": now_ms()}
    try:
        from awm.agents import agent_bus
        agent_bus.publish_act(session.scope, act)
    except Exception:  # noqa: BLE001
        pass


def publish_working(session) -> None:
    """Publish a TRANSIENT "agent working" presence act (bus-only, not persisted).

    Emitted at each turn START (the ``_input_pump`` ``send`` chokepoint), so the
    chat shows the agent is acting from the instant a turn begins — covering a
    human-initiated turn AND the supervisor's autonomous continuation (both reach
    the agent through ``send``). A stable per-scope id means a fresh working act
    REPLACES the prior one rather than stacking. The frontend clears it on the
    turn's first real content (partial / message) or on ``result``."""
    act = {"id": f"{WORKING_ACT_PREFIX}{session.scope}", "kind": "status",
           "body": "", "meta": {"phase": "working", "direction": "out",
                                "transient": True}, "ts": now_ms()}
    try:
        from awm.agents import agent_bus
        agent_bus.publish_act(session.scope, act)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Read paths
# ---------------------------------------------------------------------------

def _decode_event(row: dict) -> dict:
    try:
        meta = json.loads(row.get("meta") or "{}")
    except (TypeError, ValueError):
        return {}
    ev = meta.get("event")
    if isinstance(ev, dict):
        return ev
    return {}


def read_session(scope: str) -> list[dict]:
    """All transcript rows for ``scope``, ordered by ts."""
    return _get_dao().read_transcript(scope)


def read_acts_after(scope: str, *,
                    after_ts: int | None = None,
                    after_id: str | None = None,
                    limit: int | None = None) -> list[dict]:
    """Backfill acts after a monotonic cursor, in live wire shape.

    Each act is ``{id, kind, body, meta, ts}`` ordered by (ts, id). ``after_ts``
    None replays the whole transcript (connect with no cursor)."""
    rows = _get_dao().read_transcript_after(
        scope, after_ts=after_ts, after_id=after_id, limit=limit)
    return [_act_from_row(r) for r in rows]


def has_unmatched_tool_use(scope: str) -> bool:
    """True if the most recent assistant message contains an unmatched tool_use."""
    row = _get_dao().get_last_transcript_row(
        scope, kinds=["message", "tool_result"])
    if row is None:
        return False
    event = _decode_event(row)
    if event.get("type") != "assistant":
        return False
    content = event.get("message", {}).get("content", [])
    if not isinstance(content, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "tool_use" for b in content
    )


def read_recent_assistant_text(scope: str) -> str:
    """Concatenate the text portion of the most recent end-of-turn assistant event."""
    row = _get_dao().get_last_transcript_row(scope, kinds=["message"])
    if row is None:
        return ""
    event = _decode_event(row)
    if event.get("type") != "assistant":
        return ""
    return _extract_assistant_text(event)
