"""The ``awm`` source: one entry per tool and verb of this node's live tool table, read through ``describe``.

The table is the gateway's, so the feed holds no store of its own. Each read takes the whole table and names
it by a digest, the feed's epoch: a table that changed starts a new epoch, and search rebuilds the source.
A node another node relays for sees no singleton tool whose callers leave it out (fleet.md R98 item 3).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from awm.config import edges

NAME = "awm"
DESCRIBE_TIMEOUT_S = 10.0
_TITLE_CHARS = 200


def fingerprint(rows: list[dict[str, Any]]) -> str:
    """A digest of the ``awm services`` rows that changes whenever a service's published verbs may have."""
    keyed = sorted((str(r.get("name")), str(r.get("status")), str(r.get("id")), str(r.get("verbs")))
                   for r in rows)
    return hashlib.sha256(json.dumps(keyed).encode()).hexdigest()


def _params(params: list[dict[str, Any]]) -> str:
    lines = []
    for p in params:
        kind = p.get("type")
        marks = [", ".join(map(str, kind)) if isinstance(kind, list) else str(kind)] if kind else []
        if p.get("required"):
            marks.append("required")
        head = f"- {p.get('name')}" + (f" ({', '.join(marks)})" if marks else "")
        lines.append(f"{head}: {p['description']}" if p.get("description") else head)
    return "\n".join(lines)


def _entry(tool: str, plugin: str, tier: str, verb: dict[str, Any]) -> dict[str, Any]:
    description = str(verb.get("description") or "").strip()
    first = next((line.strip() for line in description.splitlines() if line.strip()), "")
    params = _params([p for p in verb.get("params") or [] if isinstance(p, dict)])
    text = description + (f"\n\nParams:\n{params}" if params else "")
    return {"id": f"{tool}.{verb['verb']}", "entry": {
        "title": f"{tool} {verb['verb']}: {first}"[:_TITLE_CHARS], "text": text or verb["verb"],
        "tool": tool, "verb": str(verb["verb"]), "plugin": plugin, "tier": tier}}


async def table(gateway: Any) -> list[dict[str, Any]]:
    """Every verb of the node's tools as a feed change, sorted by id. A tool whose describe fails keeps the
    one-line summaries the overview gives."""
    overview = await gateway.call("awm", "describe", {}, DESCRIBE_TIMEOUT_S)
    out: list[dict[str, Any]] = []
    for listed in (overview or {}).get("tools") or []:
        tool, tier = str(listed.get("tool") or ""), str(listed.get("tier") or "")
        summaries = [v for v in listed.get("verbs") or [] if isinstance(v, dict) and v.get("verb")]
        if not tool or not summaries:
            continue
        try:
            described = await gateway.call(tool, "describe", {"verbs": [v["verb"] for v in summaries]},
                                           DESCRIBE_TIMEOUT_S)
            verbs = [v for v in (described or {}).get("verbs") or [] if isinstance(v, dict) and v.get("verb")]
            plugin = str((described or {}).get("plugin") or listed.get("plugin") or "")
        except Exception:  # noqa: BLE001 — one tool that cannot describe itself keeps its summaries
            verbs = [{"verb": v["verb"], "description": v.get("summary") or "",
                      "params": [{"name": r, "required": True} for r in v.get("required") or []]}
                     for v in summaries]
            plugin = str(listed.get("plugin") or "")
        out.extend(_entry(tool, plugin, tier, v) for v in verbs)
    return sorted(out, key=lambda c: c["id"])


def _epoch(changes: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(changes, sort_keys=True).encode()).hexdigest()[:16]


async def export(gateway: Any, args: dict[str, Any]) -> dict[str, Any]:
    """One page of the feed. The cursor is ``<epoch>:<offset>``; a cursor from another epoch asks for a reset."""
    changes = await table(gateway)
    epoch = _epoch(changes)
    limit = max(1, min(int(args.get("limit") or 200), 1000))
    since = str(args.get("since") or "")
    offset = 0
    if since:
        named, _, at = since.rpartition(":")
        if named != epoch or not at.isdigit():
            return {"changes": [], "next": None, "done": True, "epoch": epoch, "reset": True}
        offset = int(at)
    page = changes[offset:offset + limit]
    following = offset + len(page)
    return {"changes": page, "next": f"{epoch}:{following}", "done": following >= len(changes), "epoch": epoch}


def refused(caller: str | None) -> list[str]:
    """The singleton tools a call relayed for ``caller`` may not see; none for a call made on this node."""
    if caller is None:
        return []
    return [t for t in edges.singleton_tools() if not edges.may_call(caller, t)]


def veiled(filter_: dict[str, Any], caller: str | None) -> dict[str, Any]:
    """A source filter that also leaves out the tools ``caller`` may not call."""
    hidden = refused(caller)
    if not hidden:
        return filter_
    veil = {"tool": {"$nin": hidden}}
    return {"$and": [filter_, veil]} if filter_ else veil


async def document(gateway: Any, doc_id: str, caller: str | None) -> dict[str, Any]:
    """What ``<tool>(verb="describe", args={verbs:[<verb>]})`` returns for the hit ``<tool>.<verb>``."""
    tool, _, verb = doc_id.partition(".")
    if not tool or not verb or tool in refused(caller):
        raise ValueError(f"no document {NAME}:{doc_id}")
    answer = await gateway.call(tool, "describe", {"verbs": [verb]}, DESCRIBE_TIMEOUT_S)
    if not isinstance(answer, dict) or not answer.get("verbs"):
        raise ValueError(f"no document {NAME}:{doc_id}")
    return answer
