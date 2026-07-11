"""Notifications service business logic.

The one write path is :func:`handle_report` — producers (the global Claude
Code hook, the global OpenCode plugin) POST normalized events:

    {harness, event, session_id, cwd?, transcript_path?, message?,
     last_message?, title?, reason?}

with ``event`` one of ``turn_end`` / ``notification`` / ``user_prompt`` /
``error`` / ``session_start`` / ``session_end``. Each event upserts the
session row and raises / refreshes / resolves attention items:

- ``notification`` (Claude ``Notification`` hook: permission request or
  waiting-for-input; OC ``permission.updated``) → **question**, immediately.
  The highest-signal "blocked on you" source; ``message`` becomes the detail.
- ``turn_end`` (Claude ``Stop``; OC ``session.idle``) → **question** if the
  final assistant message reads as one (see ``classify``), else **idle** with
  a grace-delayed ``notify_at`` so actively-driven sessions never desktop-ping
  every turn.
- ``error`` → **error**, immediately.
- ``user_prompt`` (the user responded to that agent) → auto-resolve all its
  open items; state back to ``working``. This is what keeps the board honest.
- ``session_start`` / ``session_end`` → lifecycle upsert; a normal end
  resolves the session's open items.

Reads run a lazy expiry sweep: a hard-killed harness never sends
``session_end``, so open items whose session went silent past the TTL are
auto-resolved as ``expired`` on the next ``list``.
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from typing import Any, Optional

from . import classify, config

IDLE_GRACE_S = float(os.environ.get("AWM_NOTIFY_IDLE_GRACE_S", "45"))
STALE_TTL_S = float(os.environ.get("AWM_NOTIFY_STALE_TTL_S", str(12 * 3600)))
SNIPPET_LEN = 280

VALID_EVENTS = {
    "turn_end", "notification", "user_prompt", "error",
    "session_start", "session_end",
    # Producer-agnostic: emitted by the fleet spawn path the instant a new agent's
    # tmux session is created, so the roster shows a "starting" row before the
    # agent has booted far enough to fire its own hook. Keyed by the tmux session
    # name (the real hook later adopts it — see handle_report).
    "spawned",
}


def _now() -> float:
    return time.time()


def _snippet(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    text = " ".join(text.split())
    return text[:SNIPPET_LEN] + ("…" if len(text) > SNIPPET_LEN else "")


def _project_label(cwd: Optional[str]) -> Optional[str]:
    """A short human label for where the session lives (last two path parts)."""
    if not cwd:
        return None
    parts = [p for p in cwd.rstrip("/").split("/") if p]
    return "/".join(parts[-2:]) if parts else None


def _row(r: sqlite3.Row) -> dict[str, Any]:
    return dict(r)


# ---------------------------------------------------------------------------
# Session + item primitives (sync, take a connection)
# ---------------------------------------------------------------------------


def upsert_session(
    conn: sqlite3.Connection,
    session_id: str,
    harness: str,
    *,
    cwd: Optional[str] = None,
    title: Optional[str] = None,
    state: Optional[str] = None,
    tmux_session: Optional[str] = None,
) -> None:
    now = _now()
    cur = conn.execute(
        "SELECT session_id FROM sessions WHERE session_id = ?", (session_id,))
    if cur.fetchone() is None:
        conn.execute(
            "INSERT INTO sessions (session_id, harness, cwd, project, title,"
            " state, tmux_session, first_seen, last_seen)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (session_id, harness, cwd, _project_label(cwd), title,
             state or "working", tmux_session, now, now),
        )
    else:
        sets = ["last_seen = ?", "harness = ?"]
        vals: list[Any] = [now, harness]
        if cwd:
            sets += ["cwd = ?", "project = ?"]
            vals += [cwd, _project_label(cwd)]
        if title:
            sets.append("title = ?")
            vals.append(title)
        if state:
            sets.append("state = ?")
            vals.append(state)
        if tmux_session:
            sets.append("tmux_session = ?")
            vals.append(tmux_session)
        vals.append(session_id)
        conn.execute(
            f"UPDATE sessions SET {', '.join(sets)} WHERE session_id = ?", vals)
    conn.commit()


def add_usage(
    conn: sqlite3.Connection,
    session_id: str,
    usage: dict[str, Any],
) -> None:
    """Fold an ``accumulate_usage`` delta into the session's cumulative counters.

    Token categories are *incremented* (the delta covers only new transcript
    bytes); ``context_tokens`` / ``model`` / ``transcript_offset`` are *set* to
    the latest observed values. A no-op when ``usage`` is falsy.
    """
    if not usage:
        return
    add = usage.get("add") or {}
    sets = [
        "tok_in = tok_in + ?",
        "tok_out = tok_out + ?",
        "tok_cache_write_5m = tok_cache_write_5m + ?",
        "tok_cache_write_1h = tok_cache_write_1h + ?",
        "tok_cache_read = tok_cache_read + ?",
        "transcript_offset = ?",
    ]
    vals: list[Any] = [
        int(add.get("tok_in") or 0),
        int(add.get("tok_out") or 0),
        int(add.get("tok_cache_write_5m") or 0),
        int(add.get("tok_cache_write_1h") or 0),
        int(add.get("tok_cache_read") or 0),
        int(usage.get("new_offset") or 0),
    ]
    if usage.get("context_tokens") is not None:
        sets.append("context_tokens = ?")
        vals.append(int(usage["context_tokens"]))
    if usage.get("model"):
        sets.append("model = ?")
        vals.append(usage["model"])
    vals.append(session_id)
    conn.execute(
        f"UPDATE sessions SET {', '.join(sets)} WHERE session_id = ?", vals)
    conn.commit()


def _session_offset(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute(
        "SELECT transcript_offset FROM sessions WHERE session_id = ?",
        (session_id,)).fetchone()
    return int(row["transcript_offset"]) if row else 0


def raise_item(
    conn: sqlite3.Connection,
    session_id: str,
    kind: str,
    title: str,
    *,
    detail: Optional[str] = None,
    snippet: Optional[str] = None,
    grace: float = 0.0,
) -> tuple[dict[str, Any], bool]:
    """Raise an attention item, or refresh the open one for (session, kind).

    Returns ``(item, created)``. The de-dupe invariant: at most one unresolved
    row per (session_id, kind) — repeat events update detail/snippet but keep
    created_at/notify_at, so the page's push gate isn't reset by re-fires.
    """
    now = _now()
    cur = conn.execute(
        "SELECT * FROM notifications WHERE session_id = ? AND kind = ?"
        " AND resolved_at IS NULL",
        (session_id, kind),
    )
    existing = cur.fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE notifications SET title = ?, detail = ?, snippet = ?"
            " WHERE id = ?",
            (title, detail if detail is not None else existing["detail"],
             snippet if snippet is not None else existing["snippet"],
             existing["id"]),
        )
        conn.commit()
        cur = conn.execute(
            "SELECT * FROM notifications WHERE id = ?", (existing["id"],))
        return _row(cur.fetchone()), False
    item_id = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO notifications (id, session_id, kind, title, detail,"
        " snippet, created_at, notify_at) VALUES (?,?,?,?,?,?,?,?)",
        (item_id, session_id, kind, title, detail, snippet, now, now + grace),
    )
    conn.commit()
    cur = conn.execute("SELECT * FROM notifications WHERE id = ?", (item_id,))
    return _row(cur.fetchone()), True


def resolve_items(
    conn: sqlite3.Connection,
    *,
    item_id: Optional[str] = None,
    session_id: Optional[str] = None,
    by: str = "page",
) -> list[str]:
    """Resolve one item (by id) or all open items for a session. Returns ids."""
    now = _now()
    if item_id:
        cur = conn.execute(
            "SELECT id FROM notifications WHERE id = ? AND resolved_at IS NULL",
            (item_id,))
    elif session_id:
        cur = conn.execute(
            "SELECT id FROM notifications WHERE session_id = ?"
            " AND resolved_at IS NULL", (session_id,))
    else:
        return []
    ids = [r["id"] for r in cur.fetchall()]
    if ids:
        qs = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE notifications SET resolved_at = ?, resolved_by = ?"
            f" WHERE id IN ({qs})", [now, by, *ids])
        conn.commit()
    return ids


def sweep_stale(conn: sqlite3.Connection, ttl: float = STALE_TTL_S) -> list[str]:
    """Expire open items whose session went silent past the TTL (no session_end
    ever arrives from a hard-killed harness)."""
    cutoff = _now() - ttl
    cur = conn.execute(
        "SELECT n.id FROM notifications n JOIN sessions s"
        " ON s.session_id = n.session_id"
        " WHERE n.resolved_at IS NULL AND s.last_seen < ?",
        (cutoff,),
    )
    ids = [r["id"] for r in cur.fetchall()]
    if ids:
        qs = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE notifications SET resolved_at = ?, resolved_by = 'expired'"
            f" WHERE id IN ({qs})", [_now(), *ids])
        conn.commit()
    return ids


# ---------------------------------------------------------------------------
# The report orchestration (async — transcript reads ride an asyncio retry)
# ---------------------------------------------------------------------------


async def handle_report(conn: sqlite3.Connection, event: dict[str, Any]) -> dict[str, Any]:
    """Ingest one normalized producer event. Returns a feed delta (or a no-op).

    Never raises on bad producer input — a hook must never learn its POST
    "failed"; malformed events are answered ``{"ok": false}`` and dropped.
    """
    ev = (event.get("event") or "").strip()
    session_id = (event.get("session_id") or "").strip()
    harness = (event.get("harness") or "unknown").strip()
    if ev not in VALID_EVENTS or not session_id:
        return {"ok": False, "error": "bad event"}

    cwd = event.get("cwd")
    title = event.get("title")
    tmux_session = event.get("tmux_session") or None
    delta: dict[str, Any] = {"ok": True, "type": None}

    # Adopt a spawn placeholder: a "spawned" event registers a transient row
    # keyed by the tmux session name; once the real agent boots and fires a hook
    # (carrying that same tmux_session under its real session_id), drop the
    # placeholder so the two don't coexist as duplicate rows.
    if ev != "spawned" and tmux_session:
        conn.execute(
            "DELETE FROM sessions WHERE tmux_session = ? AND session_id != ?"
            " AND state = 'starting'",
            (tmux_session, session_id),
        )

    if ev == "spawned":
        # Only plant the placeholder if the real row for this tmux session hasn't
        # already arrived (a fast boot can beat the spawn RPC) — else it'd dupe.
        existing = (
            conn.execute(
                "SELECT 1 FROM sessions WHERE tmux_session = ? AND session_id != ?"
                " LIMIT 1",
                (tmux_session, session_id),
            ).fetchone()
            if tmux_session else None
        )
        if existing is None:
            upsert_session(conn, session_id, harness, cwd=cwd, title=title,
                           state="starting", tmux_session=tmux_session)
        delta["type"] = "session"

    elif ev == "session_start":
        upsert_session(conn, session_id, harness, cwd=cwd, title=title,
                       state="working", tmux_session=tmux_session)
        delta["type"] = "session"

    elif ev == "user_prompt":
        upsert_session(conn, session_id, harness, cwd=cwd, state="working",
                       tmux_session=tmux_session)
        ids = resolve_items(conn, session_id=session_id, by="user_response")
        delta.update(type="resolve" if ids else "session", ids=ids)

    elif ev == "session_end":
        upsert_session(conn, session_id, harness, cwd=cwd, state="ended",
                       tmux_session=tmux_session)
        ids = resolve_items(conn, session_id=session_id, by="session_end")
        delta.update(type="resolve" if ids else "session", ids=ids)

    elif ev == "notification":
        # Permission request / waiting-for-input — blocked on the user NOW.
        # This is the single "needs you" signal (Claude also fires it on ~60s
        # idle, so a finished-and-ignored session escalates idle → needs-you
        # on its own, no NLP guess required).
        msg = event.get("message") or "Agent is waiting for your input"
        upsert_session(conn, session_id, harness, cwd=cwd, state="needs-you",
                       tmux_session=tmux_session)
        item, created = raise_item(
            conn, session_id, "needs-you", _snippet(msg) or "Needs your input",
            detail=msg, snippet=_snippet(event.get("last_message")),
        )
        delta.update(type="raise" if created else "update", item=item)

    elif ev == "error":
        msg = event.get("message") or "Agent session errored"
        upsert_session(conn, session_id, harness, cwd=cwd, state="error",
                       tmux_session=tmux_session)
        item, created = raise_item(
            conn, session_id, "error", _snippet(msg) or "Agent errored",
            detail=msg, snippet=_snippet(event.get("last_message")),
        )
        delta.update(type="raise" if created else "update", item=item)

    elif ev == "turn_end":
        # Turn ended → plain idle. No question/idle NLP fork: status is grounded
        # purely in hook signals, and a genuinely-blocked turn re-surfaces as
        # needs-you via the Notification hook.
        last = event.get("last_message")
        if last is None and event.get("transcript_path"):
            last, _tools = await classify.read_last_assistant(
                event["transcript_path"])
        upsert_session(conn, session_id, harness, cwd=cwd, state="idle",
                       tmux_session=tmux_session)
        # Fold in token / context usage from the just-appended transcript bytes.
        tpath = event.get("transcript_path")
        if tpath:
            try:
                usage = classify.accumulate_usage(
                    tpath, _session_offset(conn, session_id))
            except Exception:  # noqa: BLE001 — accounting must never break a report
                usage = None
            add_usage(conn, session_id, usage or {})
        item, created = raise_item(
            conn, session_id, "idle",
            _snippet(last) or "Agent finished and is idle",
            snippet=_snippet(last),
            grace=IDLE_GRACE_S,
        )
        delta.update(type="raise" if created else "update", item=item)

    return delta


# ---------------------------------------------------------------------------
# Read / ack verbs
# ---------------------------------------------------------------------------


def list_items(conn: sqlite3.Connection, *, all: bool = False) -> dict[str, Any]:
    sweep_stale(conn)
    if all:
        cur = conn.execute(
            "SELECT * FROM notifications ORDER BY created_at DESC LIMIT 500")
    else:
        # Open items, plus the recently-resolved hour for a "recent" strip.
        cur = conn.execute(
            "SELECT * FROM notifications WHERE resolved_at IS NULL"
            " OR resolved_at > ? ORDER BY created_at DESC LIMIT 200",
            (_now() - 3600,),
        )
    items = [_row(r) for r in cur.fetchall()]
    sids = {i["session_id"] for i in items}
    sessions: dict[str, Any] = {}
    if sids:
        qs = ",".join("?" * len(sids))
        cur = conn.execute(
            f"SELECT * FROM sessions WHERE session_id IN ({qs})", list(sids))
        sessions = {r["session_id"]: _row(r) for r in cur.fetchall()}
    return {"items": items, "sessions": sessions, "now": _now()}


def list_fleet(conn: sqlite3.Connection, *, window_s: Optional[float] = None) -> dict[str, Any]:
    """The full machine-wide roster: every recently-live session + its metrics.

    Unlike :func:`list_items` (attention items), this returns *sessions* — one
    row per agent — with cost (EOOT), context size, attach handle, and any open
    attention items folded in. Column config + spawn defaults ride along so the
    page's first paint needs a single fetch.
    """
    sweep_stale(conn)
    settings = config.FLEET_CONTRACT.load_model()
    rates = settings.rates
    divisor = settings.eoot_divisor_usd_mtok
    window = window_s if window_s is not None else settings.liveness_window_s
    now = _now()
    cutoff = now - window

    srows = conn.execute(
        "SELECT * FROM sessions WHERE last_seen >= ? ORDER BY last_seen DESC",
        (cutoff,),
    ).fetchall()

    # Open attention items grouped by session, one query.
    open_by_session: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute(
        "SELECT * FROM notifications WHERE resolved_at IS NULL"
        " ORDER BY created_at DESC"
    ).fetchall():
        open_by_session.setdefault(r["session_id"], []).append(_row(r))

    sessions: list[dict[str, Any]] = []
    for r in srows:
        d = _row(r)
        items = open_by_session.get(d["session_id"], [])
        eq = config.eoot(
            d.get("tok_in") or 0, d.get("tok_out") or 0,
            d.get("tok_cache_write_5m") or 0, d.get("tok_cache_write_1h") or 0,
            d.get("tok_cache_read") or 0,
            model=d.get("model"), rates=rates, divisor=divisor,
        )
        sessions.append({
            **d,
            "attachable": bool(d.get("tmux_session")),
            "uptime": max(0.0, now - (d.get("first_seen") or now)),
            "idle_for": max(0.0, now - (d.get("last_seen") or now)),
            "eoot": eq,
            "attention": len(items),
            "open_items": items,
        })

    return {
        "sessions": sessions,
        "now": now,
        "config": {
            "column_order": settings.column_order,
            "hidden_columns": settings.hidden_columns,
            "spawn_defaults": settings.spawn_defaults.model_dump(mode="json"),
            "notifications_enabled": settings.notifications_enabled,
        },
    }


def mark_seen(conn: sqlite3.Connection, item_id: str) -> dict[str, Any]:
    conn.execute(
        "UPDATE notifications SET seen_at = ? WHERE id = ? AND seen_at IS NULL",
        (_now(), item_id))
    conn.commit()
    return {"ok": True, "id": item_id}


def clear_all(conn: sqlite3.Connection) -> dict[str, Any]:
    cur = conn.execute(
        "SELECT id FROM notifications WHERE resolved_at IS NULL")
    ids = [r["id"] for r in cur.fetchall()]
    if ids:
        qs = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE notifications SET resolved_at = ?, resolved_by = 'cleared'"
            f" WHERE id IN ({qs})", [_now(), *ids])
        conn.commit()
    return {"ok": True, "resolved": ids}


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    open_by_kind = {
        r["kind"]: r["n"] for r in conn.execute(
            "SELECT kind, COUNT(*) AS n FROM notifications"
            " WHERE resolved_at IS NULL GROUP BY kind")
    }
    sess = {
        r["state"]: r["n"] for r in conn.execute(
            "SELECT state, COUNT(*) AS n FROM sessions GROUP BY state")
    }
    total = conn.execute("SELECT COUNT(*) AS n FROM notifications").fetchone()["n"]
    return {"open_by_kind": open_by_kind, "sessions_by_state": sess,
            "total_items": total}
