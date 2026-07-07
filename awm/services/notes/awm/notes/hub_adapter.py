"""Hub adapter for the notes service.

Boots the service as a gateway-registered process: stands up its DB (via
``dao.init`` + a startup trash purge), then runs the shared
:class:`awm.gatewayclient.ServiceAdapter` loop (register → ready → serve →
reconnect).

Surface split (enforced by the gateway via the per-function ``surfaces`` field):

- **read** verbs (``search``/``get``/``tree``/``vocab_list``) project onto MCP +
  CLI + HTTP — an agent can search your notes.
- **write / maintenance** verbs (``create``/``save``/``trash``/``restore``/
  ``purge``/``vocab_add``/``vocab_remove``) declare ``surfaces: [cli, http]`` so
  they stay off the agent MCP surface. The notes page still calls them directly
  over the unauthenticated ``/svc/notes/fn/<fn>`` proxy (that path is not the MCP
  catalog, so the gate doesn't touch it).

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.notes.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter

from awm.notes import dao, notes

log = logging.getLogger("awm.notes.hub_adapter")

_CLI_HTTP = ["cli", "http"]

API_MANIFEST: dict[str, Any] = {
    "functions": [
        # ---- read (MCP + CLI + HTTP) -------------------------------------
        {
            "name": "search",
            "tool": "notes_search",
            "description": "Search notes by keyword (FTS5), fuzzy title/content "
                           "match, or semantic similarity.",
            "params": [
                {"name": "query", "type": "string", "description": "FTS5 keyword query."},
                {"name": "fuzzy", "type": "string",
                 "description": "Typo-tolerant title/content match."},
                {"name": "semantic", "type": "string",
                 "description": "Semantic 'find notes like this' query."},
                {"name": "k", "type": "integer", "description": "Max results (default 50)."},
                {"name": "include_trashed", "type": "boolean",
                 "description": "Include trashed notes (default false)."},
            ],
        },
        {
            "name": "get",
            "tool": "notes_get",
            "description": "Fetch one note: path, timestamps, on-disk file path, content.",
            "params": [
                {"name": "id", "type": "string", "required": True},
                {"name": "include_content", "type": "boolean",
                 "description": "Include the markdown body (default true)."},
            ],
        },
        {
            "name": "tree",
            "tool": "notes_tree",
            "description": "All notes' path + metadata for the side-panel tree "
                           "(active + trashed), no content.",
            "params": [
                {"name": "include_trashed", "type": "boolean",
                 "description": "Include the trash section (default true)."},
            ],
        },
        {
            "name": "vocab_list",
            "tool": "notes_vocab_list",
            "description": "The custom dictation vocabulary (whisper hotwords).",
            "params": [],
        },
        # ---- write / maintenance (CLI + HTTP + browser proxy) ------------
        {
            "name": "create",
            "tool": "notes_create",
            "surfaces": _CLI_HTTP,
            "description": "Create a new note (uuid .md file + row).",
            "params": [
                {"name": "path", "type": "string", "description": "Title-as-path."},
                {"name": "content", "type": "string"},
            ],
        },
        {
            "name": "save",
            "tool": "notes_save",
            "surfaces": _CLI_HTTP,
            "description": "Update a note's content and/or title-path.",
            "params": [
                {"name": "id", "type": "string", "required": True},
                {"name": "content", "type": "string"},
                {"name": "path", "type": "string"},
            ],
        },
        {
            "name": "trash",
            "tool": "notes_trash",
            "surfaces": _CLI_HTTP,
            "description": "Soft-delete a note to the 30-day trash.",
            "params": [{"name": "id", "type": "string", "required": True}],
        },
        {
            "name": "restore",
            "tool": "notes_restore",
            "surfaces": _CLI_HTTP,
            "description": "Restore a note from the trash.",
            "params": [{"name": "id", "type": "string", "required": True}],
        },
        {
            "name": "purge",
            "tool": "notes_purge",
            "surfaces": _CLI_HTTP,
            "description": "Hard-delete a note now (id), or purge all trash past "
                           "the 30-day TTL (no id).",
            "params": [{"name": "id", "type": "string"}],
        },
        {
            "name": "vocab_add",
            "tool": "notes_vocab_add",
            "surfaces": _CLI_HTTP,
            "description": "Add a custom dictation term.",
            "params": [{"name": "term", "type": "string", "required": True}],
        },
        {
            "name": "vocab_remove",
            "tool": "notes_vocab_remove",
            "surfaces": _CLI_HTTP,
            "description": "Remove a custom dictation term.",
            "params": [{"name": "term", "type": "string", "required": True}],
        },
    ],
    "emitters": [],
    "sessions": [],
}


# ---------------------------------------------------------------------------
# Handlers — each opens a fresh connection to the service's own DB.
# ---------------------------------------------------------------------------


def _bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    return v in (True, "true", "True", "1", 1)


def _handle_search(args: dict) -> dict:
    conn = dao.connect()
    try:
        return notes.search(
            conn,
            query=args.get("query"),
            fuzzy=args.get("fuzzy"),
            semantic=args.get("semantic"),
            k=int(args.get("k", 50)),
            include_trashed=_bool(args.get("include_trashed")),
        )
    finally:
        conn.close()


def _handle_get(args: dict) -> dict:
    conn = dao.connect()
    try:
        return notes.get(conn, args["id"], include_content=_bool(args.get("include_content"), True))
    finally:
        conn.close()


def _handle_tree(args: dict) -> dict:
    conn = dao.connect()
    try:
        return notes.tree(conn, include_trashed=_bool(args.get("include_trashed"), True))
    finally:
        conn.close()


def _handle_vocab_list(args: dict) -> dict:
    conn = dao.connect()
    try:
        return notes.vocab_list(conn)
    finally:
        conn.close()


def _handle_create(args: dict) -> dict:
    conn = dao.connect()
    try:
        return notes.create(conn, path=args.get("path", ""), content=args.get("content", ""))
    finally:
        conn.close()


def _handle_save(args: dict) -> dict:
    conn = dao.connect()
    try:
        return notes.save(conn, args["id"], content=args.get("content"), path=args.get("path"))
    finally:
        conn.close()


def _handle_trash(args: dict) -> dict:
    conn = dao.connect()
    try:
        return notes.trash(conn, args["id"])
    finally:
        conn.close()


def _handle_restore(args: dict) -> dict:
    conn = dao.connect()
    try:
        return notes.restore(conn, args["id"])
    finally:
        conn.close()


def _handle_purge(args: dict) -> dict:
    conn = dao.connect()
    try:
        return notes.purge(conn, args.get("id"))
    finally:
        conn.close()


def _handle_vocab_add(args: dict) -> dict:
    conn = dao.connect()
    try:
        return notes.vocab_add(conn, args["term"])
    finally:
        conn.close()


def _handle_vocab_remove(args: dict) -> dict:
    conn = dao.connect()
    try:
        return notes.vocab_remove(conn, args["term"])
    finally:
        conn.close()


HANDLERS = {
    "search": _handle_search,
    "get": _handle_get,
    "tree": _handle_tree,
    "vocab_list": _handle_vocab_list,
    "create": _handle_create,
    "save": _handle_save,
    "trash": _handle_trash,
    "restore": _handle_restore,
    "purge": _handle_purge,
    "vocab_add": _handle_vocab_add,
    "vocab_remove": _handle_vocab_remove,
}


def _startup() -> None:
    """Init the DB, then sweep any trash past the 30-day TTL."""
    dao.init()
    conn = dao.connect()
    try:
        res = notes.purge_expired(conn)
        if res["purged"]:
            log.info("notes startup purge: removed %d expired note(s)", len(res["purged"]))
    finally:
        conn.close()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "notes", API_MANIFEST, HANDLERS, on_start=_startup,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
