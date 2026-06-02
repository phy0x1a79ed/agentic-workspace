"""Per-session structured transcript: read/write façade over agent_events.

Backs three callers:
  * ``agent_instances._reader_loop`` (direction='out'): one row per parsed
    stream-json event, plus a 'raw' row for JSONDecodeError fallback.
  * ``agent_instances._input_pump`` (direction='in'): one row per framed
    stdin write.
  * ``agent_instances.send_slash`` (direction='in', injection=True): one
    row per slash/compact-primer injection.

Plus a small in-process notifier so ``compact_session`` can await the
next end-of-turn assistant message without polling the DB.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from awm.db import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Event-type classification
# ---------------------------------------------------------------------------

def _classify(parsed: dict) -> str:
    """Map a parsed stream-json event to one of the canonical event_type
    buckets. Streaming partials, system-non-init, and unknown shapes all
    land in 'partial' (catch-all) — replay/compact code skips 'partial'.
    """
    t = parsed.get("type")
    if t == "system" and parsed.get("subtype") == "init":
        return "init"
    if t == "assistant":
        # `--include-partial-messages` emits separate stream_event /
        # partial shapes that don't have type='assistant'; full-turn
        # 'assistant' events carry usage and stop_reason.
        if parsed.get("partial") or parsed.get("subtype") == "partial":
            return "partial"
        return "assistant"
    if t == "user":
        # Tool-result echoes vs. genuine user posts both arrive as type='user'.
        # Inspect content blocks to distinguish.
        content = parsed.get("message", {}).get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return "tool_result"
        return "user"
    if t == "result":
        return "result"
    if t == "tool_use":
        return "tool_use"
    return "partial"


def _is_end_of_turn_assistant(parsed: dict) -> bool:
    """True iff this is a complete assistant turn worth waking compact-waiters."""
    if parsed.get("type") != "assistant":
        return False
    if parsed.get("partial") or parsed.get("subtype") == "partial":
        return False
    msg = parsed.get("message", {})
    stop_reason = msg.get("stop_reason")
    return stop_reason in ("end_turn", "stop_sequence", "tool_use")


def _extract_assistant_text(parsed: dict) -> str:
    """Concatenate the visible text portions of an assistant event."""
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
# End-of-turn notifier: compact waits on this
# ---------------------------------------------------------------------------

# session_id → asyncio.Queue[str] (assistant text payload)
_assistant_turn_subs: dict[int, set[asyncio.Queue[str]]] = {}


def subscribe_assistant_turns(session_id: int) -> asyncio.Queue[str]:
    """Get an asyncio.Queue that receives the text of each end-of-turn
    assistant event for ``session_id`` until ``unsubscribe`` is called.
    """
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=8)
    _assistant_turn_subs.setdefault(session_id, set()).add(q)
    return q


def unsubscribe_assistant_turns(session_id: int, queue: asyncio.Queue[str]) -> None:
    bucket = _assistant_turn_subs.get(session_id)
    if bucket is None:
        return
    bucket.discard(queue)
    if not bucket:
        _assistant_turn_subs.pop(session_id, None)


def _broadcast_assistant_turn(session_id: int, text: str) -> None:
    """Non-blocking fan-out. Drops on full queues (compact has 180s
    timeout — a wedged subscriber shouldn't stall the reader loop)."""
    bucket = _assistant_turn_subs.get(session_id)
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

def _next_seq(conn, session_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM agent_events WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return int(row["n"])


def _insert(conn, *, session_id: int, project: str, scope: str,
            agent_cli: str, direction: str, event_type: str,
            body_json: str, claude_session_id: str | None) -> None:
    seq = _next_seq(conn, session_id)
    conn.execute(
        "INSERT INTO agent_events "
        "(session_id, project, scope, agent_cli, seq, ts, direction, "
        "event_type, body, claude_session_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, project, scope, agent_cli, seq, _now(),
         direction, event_type, body_json, claude_session_id),
    )


def record_out(session, parsed: dict) -> None:
    """Persist one parsed stream-json event from the CLI's stdout.

    ``session`` is the AgentInstance (duck-typed: needs id/project/scope/
    agent_cli/claude_session_id). Best-effort: DB failures are swallowed
    so the reader loop never stalls on a transient write error.
    """
    event_type = _classify(parsed)
    try:
        body_json = json.dumps(parsed)
    except (TypeError, ValueError):
        body_json = json.dumps({"raw": repr(parsed)})
    try:
        conn = get_connection()
        try:
            _insert(
                conn,
                session_id=session.id, project=session.project,
                scope=session.scope, agent_cli=session.agent_cli,
                direction="out", event_type=event_type,
                body_json=body_json,
                claude_session_id=session.claude_session_id,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return
    if _is_end_of_turn_assistant(parsed):
        text = _extract_assistant_text(parsed)
        if text:
            _broadcast_assistant_turn(session.id, text)


def record_raw_out(session, line: str) -> None:
    """Persist a non-JSON stdout line as event_type='raw'."""
    try:
        conn = get_connection()
        try:
            _insert(
                conn,
                session_id=session.id, project=session.project,
                scope=session.scope, agent_cli=session.agent_cli,
                direction="out", event_type="raw",
                body_json=json.dumps({"raw": line}),
                claude_session_id=session.claude_session_id,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return


def record_in(session, body: str, *, injection: bool = False) -> None:
    """Persist a framed stdin write (room post, slash, or compact primer)."""
    try:
        body_json = json.dumps({"content": body, "injection": injection})
    except (TypeError, ValueError):
        body_json = json.dumps({"content": repr(body), "injection": injection})
    try:
        conn = get_connection()
        try:
            _insert(
                conn,
                session_id=session.id, project=session.project,
                scope=session.scope, agent_cli=session.agent_cli,
                direction="in", event_type="user",
                body_json=body_json,
                claude_session_id=session.claude_session_id,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return


# ---------------------------------------------------------------------------
# Read paths
# ---------------------------------------------------------------------------

def read_session(session_id: int, *, since_seq: int | None = None) -> list[dict]:
    """All transcript rows for ``session_id`` ordered by seq."""
    conn = get_connection()
    try:
        if since_seq is None:
            rows = conn.execute(
                "SELECT * FROM agent_events WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM agent_events "
                "WHERE session_id = ? AND seq > ? ORDER BY seq",
                (session_id, since_seq),
            ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def has_unmatched_tool_use(session_id: int) -> bool:
    """True if the most recent direction='out' event is a tool_use whose
    matching tool_result hasn't landed yet. Used by /compact to refuse
    injection mid-call.
    """
    conn = get_connection()
    try:
        # Walk backwards through out-events looking at content_block hints
        # in the most recent assistant message.
        row = conn.execute(
            "SELECT body FROM agent_events "
            "WHERE session_id = ? AND direction = 'out' "
            "AND event_type IN ('assistant', 'tool_result') "
            "ORDER BY seq DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return False
    try:
        body = json.loads(row["body"])
    except (TypeError, ValueError):
        return False
    # If the most recent terminal event is an assistant turn whose content
    # contains a tool_use block AND no tool_result has landed after it,
    # the tool is in flight. The query above already returns the LATEST
    # such event; if it's an assistant with tool_use blocks, we're mid-call.
    if body.get("type") != "assistant":
        return False
    content = body.get("message", {}).get("content", [])
    if not isinstance(content, list):
        return False
    has_tool_use = any(
        isinstance(b, dict) and b.get("type") == "tool_use" for b in content
    )
    return has_tool_use


def read_recent_assistant_text(session_id: int) -> str:
    """Concatenate the text portion of the most recent end-of-turn
    assistant event. Empty string if none exists. Used by compact as a
    fallback to extract the summary if the live subscription missed it.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT body FROM agent_events "
            "WHERE session_id = ? AND direction = 'out' "
            "AND event_type = 'assistant' "
            "ORDER BY seq DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return ""
    try:
        parsed = json.loads(row["body"])
    except (TypeError, ValueError):
        return ""
    return _extract_assistant_text(parsed)
