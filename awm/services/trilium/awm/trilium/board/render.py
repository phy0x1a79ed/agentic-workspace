"""What the writer puts in a card's notes: labels, the readable body, the record the board reads."""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Any

from awm.trilium.board.ids import COLUMN_IDS, card_note_id

#: Sections of the first board map onto the four of the new one.
SECTION_ALIASES = {"done": "history", "proposed": "history", "triage": "history"}
APPROVAL_STATES = ("pending", "approved", "rejected", "expired", "cancelled")


def section_of(card: dict[str, Any]) -> str:
    section = str(card.get("section") or "")
    section = SECTION_ALIASES.get(section, section)
    return section if section in COLUMN_IDS else "working"


def text(value: Any) -> str:
    """A string for display. An absent value is empty, never the word null."""
    if value is None:
        return ""
    return str(value)


def iso(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        return ""


def pending(card: dict[str, Any]) -> list[dict[str, Any]]:
    return [a for a in card.get("approvals") or [] if a.get("state") == "pending"]


def card_labels(card: dict[str, Any]) -> dict[str, str | None]:
    """The labels on a card's note. A None value removes the label."""
    ended = card.get("done_at") or card.get("history_at")
    values: dict[str, Any] = {
        "swarmCard": "", "readOnly": "", "taskId": card["task_id"], "section": section_of(card),
        "status": card.get("status"), "peer": card.get("peer"), "cardKind": card.get("kind"),
        "updatedAt": int(card.get("updated") or 0) or None,
        "historyAt": int(ended) if ended else None,
        "pendingApprovals": len(pending(card)) or None,
        "sourceNote": card.get("source_note"),
    }
    out: dict[str, str | None] = {}
    for key, value in values.items():
        out[key] = None if value is None or value == "None" else str(value)
    out["doneAt"] = None  # the first board's label
    return out


def card_body(card: dict[str, Any]) -> str:
    """The card's current state as HTML, so the note reads fresh in the vault."""
    e = html.escape
    meta = card.get("meta") or {}
    head = " · ".join(x for x in (section_of(card).replace("_", " "), text(card.get("status")),
                                  text(card.get("peer"))) if x)
    parts = [f"<h3>{e(head)}</h3>",
             f"<p>Task {e(card['task_id'])}, updated {e(iso(card.get('updated')))}</p>"]
    if meta.get("reason"):
        parts.append(f"<h4>Reason</h4><p>{e(text(meta['reason']))}</p>")
    todos = meta.get("todos") or []
    if todos:
        rows = "".join(f"<li>[{'x' if t.get('status') == 'completed' else ' '}] "
                       f"{e(text(t.get('subject') or t.get('content')))}</li>" for t in todos)
        parts.append(f"<h4>Todos</h4><ul>{rows}</ul>")
    sessions = card.get("sessions") or []
    if sessions:
        rows = "".join(f"<li>{e(text(s.get('name') or s.get('session_id')))} "
                       f"({e(text(s.get('role')))}, "
                       f"{'active' if s.get('active') else 'ended'})</li>" for s in sessions)
        parts.append(f"<h4>Sessions</h4><ul>{rows}</ul>")
    return "".join(parts)


def card_record(card: dict[str, Any]) -> str:
    """The JSON the board's info panel reads: sessions, approvals and the task's own detail."""
    meta = card.get("meta") or {}
    approvals = sorted(card.get("approvals") or [],
                       key=lambda a: (a.get("state") != "pending",
                                      -float(a.get("requested_at") or a.get("created") or 0)))
    return json.dumps({
        "task_id": card["task_id"], "section": section_of(card), "status": card.get("status"),
        "node": card.get("peer"), "sessions": card.get("sessions") or [], "approvals": approvals,
        "parent_task": card.get("parent_task"), "tags": card.get("tags") or [],
        "reason": meta.get("reason"), "plan": meta.get("plan"), "todos": meta.get("todos"),
        "source_note": card.get("source_note"),
    }, sort_keys=True)


def marker_html(card: dict[str, Any]) -> str:
    """The marker note under a source note: a link to the real card, with no stats."""
    e = html.escape
    nid = card_note_id(card["task_id"])
    node = f" on {e(text(card.get('peer')))}" if card.get("peer") else ""
    return (f'<p>An agent task started from this note{node}: '
            f'<a class="reference-link" href="#root/{nid}">{e(text(card.get("title")))}</a> '
            f'({e(card["task_id"])}).</p>')


def marker_title(card: dict[str, Any]) -> str:
    return f"Agent task: {text(card.get('title')) or card['task_id']}"
