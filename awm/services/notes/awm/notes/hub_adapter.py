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

from awm.notes import config, dao, notes, rooms

log = logging.getLogger("awm.notes.hub_adapter")

_CLI_HTTP = ["cli", "http"]

# Set in ``main`` once the adapter exists, so the async collab handler can fan
# merged edits out to a note's subscribers via the live control WS.
ADAPTER: ServiceAdapter | None = None

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
        # ---- live collaboration (browser only; off the agent MCP surface) --
        {
            "name": "collab_open",
            "tool": "notes_collab_open",
            "surfaces": _CLI_HTTP,
            "description": "Join a note's live room; returns {version, content}.",
            "params": [{"name": "id", "type": "string", "required": True}],
        },
        {
            "name": "collab_edit",
            "tool": "notes_collab_edit",
            "surfaces": _CLI_HTTP,
            "description": "Merge a client edit into a note's live room and fan "
                           "the merged result out to its subscribers.",
            "params": [
                {"name": "id", "type": "string", "required": True},
                {"name": "base_version", "type": "integer", "required": True},
                {"name": "content", "type": "string", "required": True},
                {"name": "client_id", "type": "string",
                 "description": "Sender's id, echoed in the broadcast so it can "
                                "skip its own edit."},
            ],
        },
    ],
    # A note's collaborators subscribe to the dynamic per-note topic
    # ``note:<id>`` (see ``config.collab_topic``); dynamic topics need no
    # manifest declaration — the hub fans out whatever the service emits.
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


def _handle_collab_open(args: dict) -> dict:
    conn = dao.connect()
    try:
        return notes.collab_open(conn, args["id"])
    finally:
        conn.close()


async def _handle_collab_edit(args: dict) -> dict:
    """Merge one client's edit, then broadcast the merged result to every
    subscriber on the note's topic. Runs on the loop thread (async) so it can
    ``emit`` on the live control WS; the merge itself is cheap."""
    res = notes.collab_edit(
        args["id"], int(args.get("base_version", 0)), args.get("content", "")
    )
    if res.get("changed") and ADAPTER is not None:
        await ADAPTER.emit(
            config.collab_topic(args["id"]),
            {
                "version": res["version"],
                "content": res["content"],
                "origin": args.get("client_id"),
            },
        )
    return res


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
    "collab_open": _handle_collab_open,
    "collab_edit": _handle_collab_edit,
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


def _flush_once() -> list[str]:
    """Persist every dirty room. Opens its own connection so it is safe to run
    in a worker thread (a sqlite connection is bound to its creating thread)."""
    conn = dao.connect()
    try:
        return rooms.flush_all(conn)
    finally:
        conn.close()


async def _flush_loop() -> None:
    """Write dirty rooms through to disk every ``config.FLUSH_INTERVAL_S``. The
    blocking DB/index work runs off the loop in a worker thread."""
    while True:
        await asyncio.sleep(config.FLUSH_INTERVAL_S)
        try:
            flushed = await asyncio.to_thread(_flush_once)
            if flushed:
                log.info("notes flush: persisted %d room(s)", len(flushed))
        except Exception:  # noqa: BLE001 — a flush failure must not kill the loop
            log.exception("notes periodic flush failed")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    global ADAPTER
    ADAPTER = ServiceAdapter("notes", API_MANIFEST, HANDLERS, on_start=_startup)
    flush_task = asyncio.create_task(_flush_loop())
    try:
        await ADAPTER.run()
    finally:
        # Graceful shutdown (the gateway's in-band shutdown frame makes run()
        # return) → drain unflushed edits so a clean stop never loses work.
        flush_task.cancel()
        try:
            flushed = await asyncio.to_thread(_flush_once)
            if flushed:
                log.info("notes shutdown flush: persisted %d room(s)", len(flushed))
        except Exception:  # noqa: BLE001
            log.exception("notes shutdown flush failed")


if __name__ == "__main__":
    asyncio.run(main())
