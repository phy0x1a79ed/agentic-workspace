"""Per-agent structured transcript: shim over ``room_transcripts``.

v36 routed CLI stdin/stdout events into a parallel ``agent_events`` table.
v37 unifies those events with the agent's owned room transcript so the UI
reads one log instead of joining two; this module is the in-process façade
that translates the agent_instances.py call sites into
``rooms_svc.post_transcript`` writes.

Callers:
  * ``agent_instances._reader_loop`` (direction='out'): one row per parsed
    stream-json event, plus a ``raw`` row for JSONDecodeError fallback.
  * ``agent_instances._input_pump`` (direction='in'): one row per framed
    stdin write.
  * ``agent_instances.send_slash`` (direction='in', injection=True): one
    row per slash/compact-primer injection.

The compact subscriber (subscribe_assistant_turns) stays in-memory only —
it's a wake signal for the compact dance, not durable state.
"""

from __future__ import annotations

import asyncio
import json

from awm.db import get_connection
from awm.services import rooms as rooms_svc

# ---------------------------------------------------------------------------
# Event-type classification (unchanged from v36)
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
# End-of-turn notifier: compact_session waits on this
# ---------------------------------------------------------------------------

# agent_instances.id (in-memory int handle) → set of asyncio.Queue[str]
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
# Owned-room resolution
# ---------------------------------------------------------------------------

def _owned_room_id(agent_id: str) -> str | None:
    """The v37 model: every active agent owns exactly one room — that's
    where its transcript lands. Returns the room id or None if the agent
    has no owned room yet (mostly during the spawn window before
    create_session has provisioned one)."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM rooms WHERE owner_agent_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Write paths
# ---------------------------------------------------------------------------

def _post_to_owned_room(*, agent_id: str, kind: str, body: str,
                        meta: dict | None) -> None:
    room_id = _owned_room_id(agent_id)
    if room_id is None:
        return
    try:
        rooms_svc.post_transcript(
            room_id, author=agent_id, kind=kind, body=body, meta=meta,
        )
    except rooms_svc.RoomError:
        # Closed/missing room — swallow; the reader loop must not stall.
        return
    except Exception:  # noqa: BLE001
        return


def record_out(session, parsed: dict) -> None:
    """Persist one parsed stream-json event from the CLI's stdout.

    ``session`` is an AgentInstance handle (duck-typed: needs ``agent_id``
    and ``id`` for the assistant-turn notifier).
    """
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
    _post_to_owned_room(
        agent_id=session.agent_id, kind=kind, body=text_body, meta=meta,
    )
    if _is_end_of_turn_assistant(parsed):
        _broadcast_assistant_turn(session.id, _extract_assistant_text(parsed))


def record_raw_out(session, line: str) -> None:
    """Persist a non-JSON stdout line as kind='system'."""
    _post_to_owned_room(
        agent_id=session.agent_id, kind="system", body=line,
        meta={"raw": True},
    )


def record_in(session, body: str, *, injection: bool = False) -> None:
    """Persist a framed stdin write (room post, slash, or compact primer)."""
    kind = "slash" if injection else "message"
    _post_to_owned_room(
        agent_id=session.agent_id, kind=kind, body=body,
        meta={"direction": "in", "injection": injection},
    )


# ---------------------------------------------------------------------------
# Read paths
# ---------------------------------------------------------------------------

def _decode_event(row) -> dict:
    """Pull the original parsed stream-json blob out of a transcript row's
    meta column. Returns {} if unreadable."""
    try:
        meta = json.loads(row["meta"] or "{}")
    except (TypeError, ValueError):
        return {}
    ev = meta.get("event")
    if isinstance(ev, dict):
        return ev
    return {}


def read_session(agent_id: str) -> list[dict]:
    """All transcript rows for ``agent_id``'s owned room, ordered by ts."""
    room_id = _owned_room_id(agent_id)
    if room_id is None:
        return []
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM room_transcripts WHERE room_id=? ORDER BY ts, id",
            (room_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def has_unmatched_tool_use(agent_id: str) -> bool:
    """True if the most recent assistant message contains a tool_use whose
    matching tool_result hasn't landed yet."""
    room_id = _owned_room_id(agent_id)
    if room_id is None:
        return False
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT meta FROM room_transcripts "
            "WHERE room_id=? AND kind IN ('message', 'tool_result') "
            "ORDER BY ts DESC, id DESC LIMIT 1",
            (room_id,),
        ).fetchone()
    finally:
        conn.close()
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


def read_recent_assistant_text(agent_id: str) -> str:
    """Concatenate the text portion of the most recent end-of-turn assistant
    event. Empty string if none exists."""
    room_id = _owned_room_id(agent_id)
    if room_id is None:
        return ""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT meta FROM room_transcripts "
            "WHERE room_id=? AND kind='message' "
            "ORDER BY ts DESC, id DESC LIMIT 1",
            (room_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return ""
    event = _decode_event(row)
    if event.get("type") != "assistant":
        return ""
    return _extract_assistant_text(event)
