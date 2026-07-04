"""Hub adapter for the precedence decision-archive service.

Boots the service as a gateway-registered process: stands up its own DB (via
``dao.init``), then runs the shared :class:`awm.gatewayclient.ServiceAdapter`
loop (register → ready → serve → reconnect).

Surface split (enforced by the gateway via the per-function ``surfaces`` field):

- **read / contribute** verbs (``search``/``get``/``stats``/``add``/``note``/
  ``vote``) project onto MCP + CLI + HTTP — agents both consume the archive of
  the user's past decisions *and* grow it (add entries, attach change-signal
  notes, vote on usefulness).
- **curation** verbs (``edit``/``supersede``/``remove``/``merge``/``import``/
  ``embed``) declare ``surfaces: [cli, http]`` so they are reachable from the host
  ``awm precedence …`` CLI (and loopback HTTP) but never advertised to spawned
  agents on the MCP surface. This is the mechanism that keeps the archive both
  self-growing and clean — it is a surface gate, not an auth boundary (the
  loopback control plane is unauthenticated by design).

Run via ``run.sh`` (which the hub spawns and respawns):
    python -m awm.precedence.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter

from awm.precedence import dao, store

log = logging.getLogger("awm.precedence.hub_adapter")

# Curation verbs — kept off the MCP surface (human curator only).
_CLI_HTTP = ["cli", "http"]

API_MANIFEST: dict[str, Any] = {
    "functions": [
        # ---- read / contribute (MCP + CLI + HTTP) ------------------------
        {
            "name": "search",
            "tool": "precedence_search",
            "description": "Look up past user-adjustment decisions by meaning. Supply "
                           "any subset of context/question/decision as independent "
                           "semantic queries (mirrors the entry shape), plus optional "
                           "keyword + tag/time filters. Ranked by usefulness (semantic "
                           "match blended with votes, seen-count, recency), not raw "
                           "cosine. Bumps seen_count on the returned entries.",
            "params": [
                {"name": "context", "type": "string",
                 "description": "Semantic query for the situation/context field."},
                {"name": "question", "type": "string",
                 "description": "Semantic query for the decision-point/question field."},
                {"name": "decision", "type": "string",
                 "description": "Semantic query for the decision/preference field."},
                {"name": "keyword", "type": "string",
                 "description": "FTS5 keyword query over the whole triple."},
                {"name": "tag", "type": "array",
                 "description": "Filter by tag (repeatable, AND-ed)."},
                {"name": "status", "type": "string",
                 "description": "Filter to a specific status (active|superseded|retired)."},
                {"name": "after", "type": "string", "description": "created on/after YYYY-MM-DD."},
                {"name": "before", "type": "string", "description": "created on/before YYYY-MM-DD."},
                {"name": "k", "type": "integer", "description": "Max results (default 20)."},
                {"name": "explore", "type": "number",
                 "description": "0..1 explore/exploit knob: 0 = single most-trusted "
                                "precedent; higher rotates under-observed entries up "
                                "(default 0.3)."},
                {"name": "include_superseded", "type": "boolean",
                 "description": "Also return superseded entries (excluded by default)."},
            ],
        },
        {
            "name": "get",
            "tool": "precedence_get",
            "description": "Fetch one decision: the triple, status, tags, votes, "
                           "seen-count, provenance, and attached notes.",
            "params": [
                {"name": "id", "type": "string", "required": True},
            ],
        },
        {
            "name": "stats",
            "tool": "precedence_stats",
            "description": "Archive summary: counts by status/source, embed coverage, "
                           "notes, total votes.",
            "params": [],
        },
        {
            "name": "add",
            "tool": "precedence_add",
            "description": "Add a user-adjustment decision: the free-text triple "
                           "(context, question, decision). Embedded per-field and "
                           "searchable immediately.",
            "params": [
                {"name": "context", "type": "string", "required": True,
                 "description": "The situation the preference applies to."},
                {"name": "question", "type": "string", "required": True,
                 "description": "The decision point that arose."},
                {"name": "decision", "type": "string", "required": True,
                 "description": "What was decided / preferred."},
                {"name": "tag", "type": "array", "description": "Free-text tags (repeatable)."},
                {"name": "created", "type": "string",
                 "description": "When the preference was formed (YYYY-MM-DD; default now)."},
            ],
        },
        {
            "name": "note",
            "tool": "precedence_note",
            "description": "Attach a note to a decision — a signal that something "
                           "changed that may affect its outcome (e.g. tooling was built).",
            "params": [
                {"name": "id", "type": "string", "required": True},
                {"name": "body", "type": "string", "required": True},
                {"name": "kind", "type": "string",
                 "description": "amendment | context-change | supersede | comment "
                                "(default comment)."},
            ],
        },
        {
            "name": "vote",
            "tool": "precedence_vote",
            "description": "Up/down vote a decision to indicate how good the entry is; "
                           "feeds the usefulness ranking.",
            "params": [
                {"name": "id", "type": "string", "required": True},
                {"name": "direction", "type": "string", "required": True,
                 "description": "up | down"},
            ],
        },
        # ---- curation (CLI + HTTP only) ----------------------------------
        {
            "name": "edit",
            "tool": "precedence_edit",
            "surfaces": _CLI_HTTP,
            "description": "Edit a decision's fields/tags (re-embeds if the triple changed).",
            "params": [
                {"name": "id", "type": "string", "required": True},
                {"name": "context", "type": "string"},
                {"name": "question", "type": "string"},
                {"name": "decision", "type": "string"},
                {"name": "created", "type": "string"},
                {"name": "add_tag", "type": "array", "description": "Tags to add."},
                {"name": "rm_tag", "type": "array", "description": "Tags to remove."},
            ],
        },
        {
            "name": "supersede",
            "tool": "precedence_supersede",
            "surfaces": _CLI_HTTP,
            "description": "Supersede/retire a decision (status flips; optional pointer "
                           "to the replacing decision + a supersede note).",
            "params": [
                {"name": "id", "type": "string", "required": True},
                {"name": "by", "type": "string",
                 "description": "Id of the replacing decision, if any."},
                {"name": "status", "type": "string",
                 "description": "superseded (default) | retired."},
                {"name": "note_body", "type": "string",
                 "description": "Optional note explaining the supersede."},
            ],
        },
        {
            "name": "remove",
            "tool": "precedence_remove",
            "surfaces": _CLI_HTTP,
            "description": "Hard-delete a decision (row, notes, tags, FTS, embeddings).",
            "params": [{"name": "id", "type": "string", "required": True}],
        },
        {
            "name": "merge",
            "tool": "precedence_merge",
            "surfaces": _CLI_HTTP,
            "description": "Absorb duplicate entries into a keeper (folds tags, notes, "
                           "votes, seen-count) — cleans up duplicate agent-added entries.",
            "params": [
                {"name": "keeper", "type": "string", "required": True},
                {"name": "dup", "type": "array", "required": True,
                 "description": "Duplicate ids to merge into the keeper."},
            ],
        },
        {
            "name": "import",
            "tool": "precedence_import",
            "surfaces": _CLI_HTTP,
            "timeout": 600,
            "description": "Load a curated staging-manifest JSON of decisions into the "
                           "archive (idempotent on a stable id; embeds afterward).",
            "params": [
                {"name": "manifest_path", "type": "string", "required": True,
                 "description": "Path to the staging-manifest JSON."},
                {"name": "embed_after", "type": "boolean",
                 "description": "Embed new/changed entries after import (default true)."},
            ],
        },
        {
            "name": "embed",
            "tool": "precedence_embed",
            "surfaces": _CLI_HTTP,
            "timeout": 600,
            "description": "Embed new/changed decisions per-field (or all, with force).",
            "params": [{"name": "force", "type": "boolean"}],
        },
    ],
    "emitters": [],
    "sessions": [],
}


# ---------------------------------------------------------------------------
# Handlers — each opens a fresh connection to the service's own DB.
# ---------------------------------------------------------------------------


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return list(v)


def _bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    return v in (True, "true", "True", "1", 1)


def _handle_search(args: dict) -> dict:
    conn = dao.connect()
    try:
        return store.search(
            conn,
            context=args.get("context"),
            question=args.get("question"),
            decision=args.get("decision"),
            keyword=args.get("keyword"),
            tag=_as_list(args.get("tag")),
            status=args.get("status"),
            after=args.get("after"),
            before=args.get("before"),
            k=int(args.get("k", 20)),
            explore=args.get("explore"),
            include_superseded=_bool(args.get("include_superseded")),
        )
    finally:
        conn.close()


def _handle_get(args: dict) -> dict:
    conn = dao.connect()
    try:
        return store.get(conn, args["id"])
    finally:
        conn.close()


def _handle_stats(args: dict) -> dict:
    conn = dao.connect()
    try:
        return store.stats(conn)
    finally:
        conn.close()


def _handle_add(args: dict) -> dict:
    conn = dao.connect()
    try:
        return store.add(
            conn,
            context=args["context"],
            question=args["question"],
            decision=args["decision"],
            tag=_as_list(args.get("tag")),
            source=args.get("source", "agent"),
            source_ref=args.get("source_ref"),
            created=args.get("created"),
        )
    finally:
        conn.close()


def _handle_note(args: dict) -> dict:
    conn = dao.connect()
    try:
        return store.note(conn, args["id"], body=args["body"],
                          kind=args.get("kind", "comment"),
                          author=args.get("author", "agent"))
    finally:
        conn.close()


def _handle_vote(args: dict) -> dict:
    conn = dao.connect()
    try:
        return store.vote(conn, args["id"], direction=args["direction"])
    finally:
        conn.close()


def _handle_edit(args: dict) -> dict:
    conn = dao.connect()
    try:
        return store.edit(
            conn, args["id"],
            context=args.get("context"),
            question=args.get("question"),
            decision=args.get("decision"),
            created=args.get("created"),
            add_tag=_as_list(args.get("add_tag")),
            rm_tag=_as_list(args.get("rm_tag")),
        )
    finally:
        conn.close()


def _handle_supersede(args: dict) -> dict:
    conn = dao.connect()
    try:
        return store.supersede(conn, args["id"], by=args.get("by"),
                               status=args.get("status", "superseded"),
                               note_body=args.get("note_body"))
    finally:
        conn.close()


def _handle_remove(args: dict) -> dict:
    conn = dao.connect()
    try:
        return store.remove(conn, args["id"])
    finally:
        conn.close()


def _handle_merge(args: dict) -> dict:
    conn = dao.connect()
    try:
        return store.merge(conn, args["keeper"], _as_list(args.get("dup")))
    finally:
        conn.close()


def _handle_import(args: dict) -> dict:
    conn = dao.connect()
    try:
        return store.import_manifest(conn, manifest_path=args["manifest_path"],
                                     embed_after=_bool(args.get("embed_after"), True))
    finally:
        conn.close()


def _handle_embed(args: dict) -> dict:
    conn = dao.connect()
    try:
        return store.embed(conn, force=_bool(args.get("force")))
    finally:
        conn.close()


HANDLERS = {
    "search": _handle_search,
    "get": _handle_get,
    "stats": _handle_stats,
    "add": _handle_add,
    "note": _handle_note,
    "vote": _handle_vote,
    "edit": _handle_edit,
    "supersede": _handle_supersede,
    "remove": _handle_remove,
    "merge": _handle_merge,
    "import": _handle_import,
    "embed": _handle_embed,
}


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "precedence", API_MANIFEST, HANDLERS, on_start=dao.init,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
