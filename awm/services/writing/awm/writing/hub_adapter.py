"""Hub adapter for the writing corpus service.

Boots the service as a gateway-registered process: stands up its own DB (via
``dao.init``), then runs the shared :class:`awm.gatewayclient.ServiceAdapter`
loop (register → ready → serve → reconnect).

Surface split (enforced by the gateway via the per-function ``surfaces`` field):

- **read** verbs (``search``/``get``/``stats``/``vocab``/``feed``) project onto
  MCP + CLI + HTTP — any agent can search the corpus to match the author's voice.
- **write / maintenance** verbs (``add``/``retag``/``remove``/``link_dup``/
  ``unlink_dup``/``import``/``embed``/``dedup``) declare ``surfaces: [cli, http]``
  so they are reachable from the host ``awm`` CLI (and loopback HTTP) but never
  advertised to spawned agents on the MCP surface. This is a surface gate, not an
  auth boundary — the loopback control plane is unauthenticated by design.

Run via ``run.sh`` (which the hub spawns and respawns):
    python -m awm.writing.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter

from awm.writing import corpus, dao

log = logging.getLogger("awm.writing.hub_adapter")

# Verbs that mutate the corpus or run maintenance — kept off the MCP surface.
_CLI_HTTP = ["cli", "http"]

API_MANIFEST: dict[str, Any] = {
    "functions": [
        # ---- read (MCP + CLI + HTTP) -------------------------------------
        {
            "name": "search",
            "tool": "writing_search",
            "description": "Search the writing corpus by keyword (FTS5), semantic "
                           "similarity, or hybrid, with metadata + tag filters.",
            "params": [
                {"name": "query", "type": "string",
                 "description": "FTS5 keyword query."},
                {"name": "semantic", "type": "string",
                 "description": "Semantic 'find samples like this' query."},
                {"name": "k", "type": "integer", "description": "Max results (default 20)."},
                {"name": "type", "type": "string",
                 "description": "Filter by type (academic/creative-personal/professional/...)."},
                {"name": "tag", "type": "array",
                 "description": "Filter by tag facet:value (repeatable, AND-ed)."},
                {"name": "after", "type": "string", "description": "created on/after YYYY-MM-DD."},
                {"name": "min_words", "type": "integer", "description": "Minimum wordcount."},
                {"name": "grade", "type": "string",
                 "description": "Filter by grade: style-grade|acceptable|weak."},
                {"name": "include_dups", "type": "boolean",
                 "description": "Include samples marked as duplicates."},
            ],
        },
        {
            "name": "get",
            "tool": "writing_get",
            "description": "Fetch one sample: metadata, tags, dup status, and full text.",
            "params": [
                {"name": "id", "type": "string", "required": True},
                {"name": "include_text", "type": "boolean",
                 "description": "Include the full sample text (default true)."},
            ],
        },
        {
            "name": "stats",
            "tool": "writing_stats",
            "description": "Corpus summary: counts by type/grade, embed coverage, untagged.",
            "params": [],
        },
        {
            "name": "vocab",
            "tool": "writing_vocab",
            "description": "The closed tag vocabulary (valid form/register/grade values).",
            "params": [],
        },
        {
            "name": "feed",
            "tool": "writing_feed",
            "description": "Style-reference feed: full-text records for the "
                           "style-grade non-duplicate keepers (filterable) — the "
                           "voice-matching feed.",
            "params": [
                {"name": "grade", "type": "string",
                 "description": "Grade filter (defaults to style-grade)."},
                {"name": "tag", "type": "array", "description": "Filter by tag facet:value."},
                {"name": "type", "type": "string", "description": "Filter by type."},
                {"name": "after", "type": "string", "description": "created on/after YYYY-MM-DD."},
                {"name": "min_words", "type": "integer", "description": "Minimum wordcount."},
            ],
        },
        # ---- write / maintenance (CLI + HTTP only) -----------------------
        {
            "name": "add",
            "tool": "writing_add",
            "surfaces": _CLI_HTTP,
            "description": "Add a new sample from inline text (stored in-DB + embedded).",
            "params": [
                {"name": "text", "type": "string", "required": True},
                {"name": "name", "type": "string"},
                {"name": "type", "type": "string", "description": "Corpus type (default unsorted)."},
                {"name": "tag", "type": "array", "description": "Tags facet:value (repeatable)."},
            ],
        },
        {
            "name": "retag",
            "tool": "writing_retag",
            "surfaces": _CLI_HTTP,
            "description": "Add/remove tags on a sample.",
            "params": [
                {"name": "id", "type": "string", "required": True},
                {"name": "add", "type": "array", "description": "Tags to add (facet:value)."},
                {"name": "rm", "type": "array", "description": "Tags to remove (facet:value)."},
            ],
        },
        {
            "name": "remove",
            "tool": "writing_remove",
            "surfaces": _CLI_HTTP,
            "description": "Remove a sample (drops its row, tags, FTS, and embedding).",
            "params": [{"name": "id", "type": "string", "required": True}],
        },
        {
            "name": "link_dup",
            "tool": "writing_link_dup",
            "surfaces": _CLI_HTTP,
            "description": "Mark reviewed duplicates against a keeper.",
            "params": [
                {"name": "keeper", "type": "string", "required": True},
                {"name": "dup", "type": "array", "required": True,
                 "description": "Duplicate ids to link to the keeper."},
            ],
        },
        {
            "name": "unlink_dup",
            "tool": "writing_unlink_dup",
            "surfaces": _CLI_HTTP,
            "description": "Clear duplicate status on a sample.",
            "params": [{"name": "id", "type": "string", "required": True}],
        },
        {
            "name": "import",
            "tool": "writing_import",
            "surfaces": _CLI_HTTP,
            "timeout": 600,
            "description": "Bulk-ingest a manifest.json + text-file corpus into the "
                           "service DB (defaults to the historical corpus dir).",
            "params": [
                {"name": "corpus_dir", "type": "string",
                 "description": "Directory holding manifest.json + sample files."},
            ],
        },
        {
            "name": "embed",
            "tool": "writing_embed",
            "surfaces": _CLI_HTTP,
            "timeout": 600,
            "description": "Embed new/changed samples (or all, with force).",
            "params": [{"name": "force", "type": "boolean"}],
        },
        {
            "name": "dedup",
            "tool": "writing_dedup",
            "surfaces": _CLI_HTTP,
            "timeout": 300,
            "description": "Content-based duplicate detection; pass apply to mark clusters.",
            "params": [{"name": "apply", "type": "boolean"}],
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


def _bool(v: Any) -> bool:
    return v in (True, "true", "True", "1", 1)


def _handle_search(args: dict) -> dict:
    conn = dao.connect()
    try:
        return corpus.search(
            conn,
            query=args.get("query"),
            semantic=args.get("semantic"),
            k=int(args.get("k", 20)),
            type=args.get("type"),
            tag=_as_list(args.get("tag")),
            after=args.get("after"),
            min_words=args.get("min_words"),
            grade=args.get("grade"),
            include_dups=_bool(args.get("include_dups")),
        )
    finally:
        conn.close()


def _handle_get(args: dict) -> dict:
    conn = dao.connect()
    try:
        return corpus.get(conn, args["id"], include_text=_bool(args.get("include_text", True)))
    finally:
        conn.close()


def _handle_stats(args: dict) -> dict:
    conn = dao.connect()
    try:
        return corpus.stats(conn)
    finally:
        conn.close()


def _handle_vocab(args: dict) -> dict:
    return corpus.vocab()


def _handle_feed(args: dict) -> dict:
    conn = dao.connect()
    try:
        return corpus.feed(
            conn,
            grade=args.get("grade"),
            tag=_as_list(args.get("tag")),
            type=args.get("type"),
            after=args.get("after"),
            min_words=args.get("min_words"),
        )
    finally:
        conn.close()


def _handle_add(args: dict) -> dict:
    conn = dao.connect()
    try:
        return corpus.add(
            conn,
            text=args["text"],
            name=args.get("name"),
            type=args.get("type", "unsorted"),
            tag=_as_list(args.get("tag")),
        )
    finally:
        conn.close()


def _handle_retag(args: dict) -> dict:
    conn = dao.connect()
    try:
        return corpus.retag(conn, args["id"],
                            add=_as_list(args.get("add")), rm=_as_list(args.get("rm")))
    finally:
        conn.close()


def _handle_remove(args: dict) -> dict:
    conn = dao.connect()
    try:
        return corpus.remove(conn, args["id"])
    finally:
        conn.close()


def _handle_link_dup(args: dict) -> dict:
    conn = dao.connect()
    try:
        return corpus.link_dup(conn, args["keeper"], _as_list(args.get("dup")))
    finally:
        conn.close()


def _handle_unlink_dup(args: dict) -> dict:
    conn = dao.connect()
    try:
        return corpus.unlink_dup(conn, args["id"])
    finally:
        conn.close()


def _handle_import(args: dict) -> dict:
    conn = dao.connect()
    try:
        return corpus.import_corpus(conn, corpus_dir=args.get("corpus_dir"))
    finally:
        conn.close()


def _handle_embed(args: dict) -> dict:
    conn = dao.connect()
    try:
        return corpus.embed(conn, force=_bool(args.get("force")))
    finally:
        conn.close()


def _handle_dedup(args: dict) -> dict:
    conn = dao.connect()
    try:
        return corpus.dedup(conn, apply=_bool(args.get("apply")))
    finally:
        conn.close()


HANDLERS = {
    "search": _handle_search,
    "get": _handle_get,
    "stats": _handle_stats,
    "vocab": _handle_vocab,
    "feed": _handle_feed,
    "add": _handle_add,
    "retag": _handle_retag,
    "remove": _handle_remove,
    "link_dup": _handle_link_dup,
    "unlink_dup": _handle_unlink_dup,
    "import": _handle_import,
    "embed": _handle_embed,
    "dedup": _handle_dedup,
}


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "writing", API_MANIFEST, HANDLERS, on_start=dao.init,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
