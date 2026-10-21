"""Exchanging messages with a running workbench conversation, awm-initiated.

The workbench cannot reach awm from its sandbox (see ``mcp_bridge``), so the
traffic runs the other way: awm posts into a conversation through the loopback
API and polls it for replies. A conversation is a *root frame*; its transcript
is an append-only list, so a position in that list is a stable cursor.

The routes below were read out of the 0.1.x binary and confirmed with GETs on a
live workbench. ``POST /api/frames/:id/message`` has only been read, never
called, so its body shape is unverified live. ``POST /api/projects/:pid/request``
answers ``{root_frame_id, frame_id, status: "accepted"}`` and runs the first
turn in the background.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from awm.claude_science.api import Owner, WorkbenchError

LIST_LIMIT = 50
TAIL_LIMIT = 20
PAGE_LIMIT = 200


def list_conversations(owner: Owner, *, project_id: str | None = None,
                       limit: int = LIST_LIMIT) -> list[dict[str, Any]]:
    """Visible root conversations, most recently updated first."""
    params: dict[str, Any] = {"root_only": "true", "limit": limit}
    if project_id:
        params["project_id"] = project_id
    rows = owner.get("/api/frames", params=params)
    rows = [r for r in rows if not r.get("is_hidden")]
    return sorted(rows, key=lambda r: r.get("updated_at") or "", reverse=True)


def summary(frame: dict[str, Any]) -> dict[str, Any]:
    return {k: frame.get(k) for k in
            ("id", "name", "status", "project_id", "updated_at")}


def resolve_conversation(owner: Owner, ref: str | None = None, *,
                         project_id: str | None = None) -> dict[str, Any]:
    """The conversation ``ref`` names, or the latest one when ``ref`` is empty.

    ``ref`` is matched as an id, an id prefix, an exact name, then a unique
    name substring, each case-insensitively. Ambiguity is an error that lists
    the candidates, because posting into the wrong conversation is not
    something a retry undoes.
    """
    rows = list_conversations(owner, project_id=project_id)
    if not rows:
        raise WorkbenchError("the workbench has no conversations")
    ref = (ref or "").strip().lower()
    if not ref:
        return rows[0]
    tiers = (
        lambda r: (r.get("id") or "").lower() == ref,
        lambda r: (r.get("id") or "").lower().startswith(ref),
        lambda r: (r.get("name") or "").lower() == ref,
        lambda r: ref in (r.get("name") or "").lower(),
    )
    for match in tiers:
        hits = [r for r in rows if match(r)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            names = "; ".join(f"{h['id'][:8]} {h.get('name')!r}" for h in hits[:6])
            raise WorkbenchError(
                f"{ref!r} matches {len(hits)} conversations: {names}")
    raise WorkbenchError(f"no conversation matches {ref!r}")


def post_message(owner: Owner, frame_id: str, text: str) -> dict[str, Any]:
    """Send ``text`` as the user's next turn; the daemon queues it if busy.

    ``cursor`` is the transcript length before the send, so a read from it
    returns the sent message and everything after.
    """
    page = owner.get(f"/api/frames/{frame_id}/messages", params={"limit": 1})
    sent = owner.post(f"/api/frames/{frame_id}/message",
                      json={"input_data": {"request": text}})
    return {"result": sent, "cursor": page.get("total")}


def open_conversation(owner: Owner, text: str, *, project: str,
                      model: str | None = None, effort: str | None = None
                      ) -> dict[str, Any]:
    """Start a new conversation in ``project`` (found or created by name) with
    ``text`` as its first turn.

    The create is never retried: a request that fails in flight may still have
    started the conversation, and a second one would start it twice.
    """
    proj = owner.post("/api/projects", json={"name": project, "find_or_create": True})
    pid = proj.get("project_id")
    if not pid:
        raise WorkbenchError(f"no project_id for project {project!r}: {proj}")
    body: dict[str, Any] = {"input_data": {"request": text}}
    if model:
        body["model"] = model
    if effort:
        body["effort"] = effort
    try:
        sent = owner.post(f"/api/projects/{pid}/request", json=body)
    except WorkbenchError as exc:
        if exc.status is None:
            raise WorkbenchError(
                f"{exc} — the conversation may have started anyway; list the "
                f"project with `science read` before trying again") from exc
        raise
    return {
        "conversation": {"id": sent.get("root_frame_id"), "project_id": pid,
                         "project": proj.get("name") or project},
        "status": sent.get("status"),
        "cursor": 0,
    }


def _iso(ms: Any) -> str | None:
    if not isinstance(ms, (int, float)):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(
        timespec="milliseconds")


def _plain(message: dict[str, Any], index: int) -> dict[str, Any]:
    content = message.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    blocks = [b for b in content or [] if isinstance(b, dict)]
    out: dict[str, Any] = {
        "index": index,
        "role": message.get("role"),
        "ts": _iso(message.get("_ts")),
        "text": "\n".join(b.get("text") or "" for b in blocks
                          if b.get("type") == "text").strip(),
    }
    tools = [b.get("name") for b in blocks if b.get("type") == "tool_use"]
    if tools:
        out["tool_calls"] = tools
    if any(b.get("type") == "tool_result" for b in blocks):
        out["tool_results"] = sum(b.get("type") == "tool_result" for b in blocks)
    if message.get("_harness_notice"):
        out["harness_notice"] = True
    return out


def read_messages(owner: Owner, frame_id: str, *, since: int | None = None,
                  limit: int | None = None, include_tools: bool = False
                  ) -> dict[str, Any]:
    """Transcript entries from index ``since`` on, with the next cursor.

    With no ``since`` this is the tail of the transcript, so a first read
    orients without paging the whole history. Entries that carry no text (tool
    plumbing) are dropped unless ``include_tools``; the cursor still passes them.
    """
    if since is None:
        page = owner.get(f"/api/frames/{frame_id}/messages",
                         params={"limit": limit or TAIL_LIMIT})
    else:
        page = owner.get(f"/api/frames/{frame_id}/messages",
                         params={"from": max(0, since),
                                 "limit": limit or PAGE_LIMIT})
    start = int(page.get("from") or 0)
    raw = page.get("messages") or []
    entries = [_plain(m, start + i) for i, m in enumerate(raw)]
    if not include_tools:
        entries = [e for e in entries if e["text"]]
    frame = owner.get(f"/api/frames/{frame_id}", params={"shallow": "true"})
    return {
        "conversation": summary(frame),
        "messages": entries,
        "cursor": start + len(raw),
        "total": page.get("total"),
        "more": start + len(raw) < int(page.get("total") or 0),
    }
