"""Hub adapter for the notes service.

Boots the service as a gateway-registered process: stands up its DB (via
``dao.init`` + a startup trash purge), then runs the shared
:class:`awm.gatewayclient.ServiceAdapter` loop (register → ready → serve →
reconnect).

Surface split (enforced by the gateway via the per-function ``surfaces`` field).
Nearly everything omits ``surfaces``, which projects it onto MCP + CLI + HTTP
alike: the three surfaces carry the same verbs, so the tool an agent can see is
the tool it should reach for. Writing is safe to expose because every write goes
through the checkout contract (:mod:`awm.notes.checkout`) rather than around it.

Two verbs are the exception. ``collab_open``/``collab_edit`` declare
``surfaces: [cli, http]`` — they are a keystroke-level browser protocol carrying
a room version, and putting them on the agent surface would only be a way to
corrupt a note. The notes page reaches them over the unauthenticated
``/svc/notes/fn/<fn>`` proxy, which is not the MCP catalog, so the gate does not
touch it. Drawio gates its editor protocol the same way and for the same reason.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.notes.hub_adapter
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

from awm.config import autocommit, userroot
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
                           "match, or semantic similarity. Rows carry `rev` (what "
                           "to write back against) and `file_path` (for reading "
                           "only — writing it loses the edit, see `checkout`).",
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
            "description": "Fetch one note: path, timestamps, content, and the "
                           "revision that content is at. `file_path` is where the "
                           "body happens to be stored — read it if you like, but "
                           "never write it: an open editor holds the live copy in "
                           "memory and will overwrite your edit without a word. "
                           "Use `checkout` to edit, or `save` with this `rev`.",
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
                           "(active + trashed), no content. `file_path` is a read "
                           "location, not a write target — edit through `checkout`.",
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
        # ---- write / maintenance (MCP + CLI + HTTP + browser proxy) ------
        {
            "name": "create",
            "tool": "notes_create",
            "description": "Create a new note. Pass checkout to get a working "
                           "copy of it back in the same call.",
            "params": [
                {"name": "path", "type": "string", "description": "Title-as-path."},
                {"name": "content", "type": "string"},
                {"name": "checkout", "type": "boolean",
                 "description": "Also take a checkout of the new note (default false)."},
            ],
        },
        {
            "name": "save",
            "tool": "notes_save",
            "timeout": 120,
            "description": "Write a note's content and/or title-path in one call. "
                           "The content is MERGED with whatever the note holds "
                           "now, not substituted for it — so an edit composed "
                           "against an older copy folds in rather than erasing "
                           "what happened since, and a real conflict is refused "
                           "rather than guessed. Pass the `rev` you read as "
                           "base_rev to make the merge base exact. For anything "
                           "bigger than a one-shot write, take a `checkout`.",
            "params": [
                {"name": "id", "type": "string", "required": True},
                {"name": "content", "type": "string"},
                {"name": "path", "type": "string"},
                {"name": "base_rev", "type": "string",
                 "description": "The rev this content was composed against, from "
                                "get/search/tree. Omitted, the note's file is "
                                "taken as the merge base."},
            ],
        },
        {
            "name": "trash",
            "tool": "notes_trash",
            "description": "Soft-delete a note to the 30-day trash.",
            "params": [{"name": "id", "type": "string", "required": True}],
        },
        {
            "name": "restore",
            "tool": "notes_restore",
            "description": "Restore a note from the trash.",
            "params": [{"name": "id", "type": "string", "required": True}],
        },
        {
            "name": "purge",
            "tool": "notes_purge",
            "description": "Hard-delete a note now (id), or purge all trash past "
                           "the 30-day TTL (no id). The only unrecoverable verb "
                           "here — everything else goes through the trash.",
            "params": [{"name": "id", "type": "string"}],
        },
        {
            "name": "vocab_add",
            "tool": "notes_vocab_add",
            "description": "Add a custom dictation term.",
            "params": [{"name": "term", "type": "string", "required": True}],
        },
        {
            "name": "vocab_remove",
            "tool": "notes_vocab_remove",
            "description": "Remove a custom dictation term.",
            "params": [{"name": "term", "type": "string", "required": True}],
        },
        {
            "name": "reindex",
            "tool": "notes_reindex",
            # The first embed in a fresh process downloads/loads the model, so
            # the default 30s RPC budget is nowhere near enough.
            "timeout": 600,
            "description": "Re-embed notes whose content changed since their "
                           "last embed (or all, with force).",
            "params": [{"name": "force", "type": "boolean"}],
        },
        # ---- checkouts (MCP + CLI + HTTP) --------------------------------
        {
            "name": "checkout",
            "tool": "notes_checkout",
            "description": "Take a working copy of a note and get back a handle. "
                           "Edit it for as long as you like while someone types "
                           "into the live note in the browser; nothing you do is "
                           "visible to them, and nothing they do disturbs your "
                           "copy, until you merge. Pass the handle to path / read "
                           "/ write / status / update / merge / discard.",
            "params": [{"name": "id", "type": "string", "required": True}],
        },
        {
            "name": "path",
            "tool": "notes_path",
            "description": "The filesystem path of a checkout's working copy — "
                           "edit it with whatever tool you like. Never edit a LIVE "
                           "note's file this way; that is the race this contract "
                           "exists to remove.",
            "params": [{"name": "handle", "type": "string", "required": True}],
        },
        {
            "name": "read",
            "tool": "notes_read",
            "description": "The working copy's text, for a caller that cannot "
                           "reach the filesystem.",
            "params": [{"name": "handle", "type": "string", "required": True}],
        },
        {
            "name": "write",
            "tool": "notes_write",
            "description": "Replace the working copy's text. Local to your "
                           "checkout until you merge.",
            "params": [
                {"name": "handle", "type": "string", "required": True},
                {"name": "content", "type": "string", "required": True},
            ],
        },
        {
            "name": "status",
            "tool": "notes_status",
            "description": "Where a checkout stands: whether it has changes to "
                           "land (ahead), whether the live note has moved since "
                           "(behind), and whether unresolved conflict markers "
                           "remain.",
            "params": [{"name": "handle", "type": "string", "required": True}],
        },
        {
            "name": "update",
            "tool": "notes_update",
            "timeout": 120,
            "description": "Pull the live note's changes into your checkout. "
                           "Clean merges just apply. Genuine conflicts are "
                           "REPORTED, not guessed: the file gets ordinary "
                           "<<<<<<< markers, you edit it by hand at `path`, then "
                           "call `resolve`. This is the only place reconciliation "
                           "happens — by the time you merge, landing is atomic "
                           "and cannot produce a note neither side asked for.",
            "params": [{"name": "handle", "type": "string", "required": True}],
        },
        {
            "name": "resolve",
            "tool": "notes_resolve",
            "description": "Declare a hand-resolved checkout clean. Refuses while "
                           "conflict markers remain.",
            "params": [{"name": "handle", "type": "string", "required": True}],
        },
        {
            "name": "merge",
            "tool": "notes_merge",
            "timeout": 120,
            "description": "Land your checkout onto the live note as one "
                           "transaction, and push the result to every open editor "
                           "so it appears without anyone typing. Refuses if the "
                           "checkout is behind or conflicted — call update first.",
            "params": [
                {"name": "handle", "type": "string", "required": True},
                {"name": "keep", "type": "boolean",
                 "description": "Keep the checkout open after landing (default false)."},
            ],
        },
        {
            "name": "discard",
            "tool": "notes_discard",
            "description": "Throw away a checkout and everything in it.",
            "params": [{"name": "handle", "type": "string", "required": True}],
        },
        {
            "name": "checkouts",
            "tool": "notes_checkouts",
            "description": "Every checkout currently open, optionally for one note.",
            "params": [{"name": "id", "type": "string"}],
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


def _handle_create(args: dict, as_: str | None = None) -> dict:
    conn = dao.connect()
    try:
        return notes.create(
            conn,
            path=args.get("path", ""),
            content=args.get("content", ""),
            checkout=_bool(args.get("checkout")),
            author=_author(as_),
        )
    finally:
        conn.close()


def _author(as_: str | None) -> str:
    """Who took this checkout. The gateway threads caller identity as ``as_``."""
    return as_ or "agent"


async def _emit_landed(note_id: str, version: int | None) -> None:
    """Tell every open editor the note moved.

    Without this a landed write sits invisible in an open tab until someone
    types. The browser folds an incoming ``{version, content}`` in by diffing
    against its own shadow, so keystrokes made while the write landed survive
    rather than being replaced by it.
    """
    if version is None or ADAPTER is None:
        return
    await ADAPTER.emit(
        config.collab_topic(note_id),
        {"version": version, "content": rooms.live_text(note_id), "origin": "server"},
    )


def _save(args: dict) -> dict:
    conn = dao.connect()
    try:
        return notes.save(
            conn, args["id"],
            content=args.get("content"),
            path=args.get("path"),
            base_rev=args.get("base_rev"),
        )
    finally:
        conn.close()


async def _handle_save(args: dict) -> dict:
    res = await asyncio.to_thread(_save, args)
    await _emit_landed(args["id"], res.get("version"))
    return res


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


def _handle_reindex(args: dict) -> dict:
    conn = dao.connect()
    try:
        return notes.reindex(conn, force=bool(args.get("force")))
    finally:
        conn.close()


# ---- checkouts -------------------------------------------------------------


def _handle_checkout(args: dict, as_: str | None = None) -> dict:
    conn = dao.connect()
    try:
        return notes.take_checkout(conn, args["id"], author=_author(as_))
    finally:
        conn.close()


def _handle_checkouts(args: dict) -> dict:
    return notes.list_checkouts(args.get("id"))


def _handle_path(args: dict) -> dict:
    return notes.checkout_path(args["handle"])


def _handle_read(args: dict) -> dict:
    return notes.checkout_read(args["handle"])


def _handle_write(args: dict) -> dict:
    return notes.checkout_write(args["handle"], args.get("content", ""))


def _handle_status(args: dict) -> dict:
    return notes.checkout_status(args["handle"])


def _handle_update(args: dict) -> dict:
    return notes.checkout_update(args["handle"])


def _handle_resolve(args: dict) -> dict:
    return notes.checkout_resolve(args["handle"])


def _merge(args: dict) -> dict:
    conn = dao.connect()
    try:
        return notes.checkout_merge(conn, args["handle"], keep=_bool(args.get("keep")))
    finally:
        conn.close()


async def _handle_merge(args: dict) -> dict:
    res = await asyncio.to_thread(_merge, args)
    await _emit_landed(res["note_id"], res.get("version"))
    return res


def _handle_discard(args: dict) -> dict:
    return notes.checkout_discard(args["handle"])


def _handle_collab_open(args: dict) -> dict:
    conn = dao.connect()
    try:
        res = notes.collab_open(conn, args["id"])
    finally:
        conn.close()
    res["topic"] = config.collab_topic(args["id"])
    return res


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


def _bound(handler):
    """Run ``handler`` with the caller's user bound (``awm.config.userroot``).

    The gateway threads the edge-verified ``X-Awm-As`` as ``as_``; a known user
    binds their store for the call, anything else means the legacy store (or,
    in strict mode, a refusal). The binding is a ContextVar, so it follows the
    handler into ``asyncio.to_thread``.
    """
    two = len(inspect.signature(handler).parameters) >= 2

    if inspect.iscoroutinefunction(handler):
        async def _async(args: dict, as_: str | None = None):
            with userroot.bind(userroot.resolve(as_)):
                return await (handler(args, as_) if two else handler(args))
        _async.__name__ = handler.__name__
        return _async

    def _sync(args: dict, as_: str | None = None):
        with userroot.bind(userroot.resolve(as_)):
            return handler(args, as_) if two else handler(args)
    _sync.__name__ = handler.__name__
    return _sync


_HANDLERS = {
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
    "reindex": _handle_reindex,
    "checkout": _handle_checkout,
    "checkouts": _handle_checkouts,
    "path": _handle_path,
    "read": _handle_read,
    "write": _handle_write,
    "status": _handle_status,
    "update": _handle_update,
    "resolve": _handle_resolve,
    "merge": _handle_merge,
    "discard": _handle_discard,
    "collab_open": _handle_collab_open,
    "collab_edit": _handle_collab_edit,
}
HANDLERS = {name: _bound(h) for name, h in _HANDLERS.items()}


def _purge_expired() -> None:
    conn = dao.connect()
    try:
        res = notes.purge_expired(conn)
        if res["purged"]:
            log.info("notes startup purge (%s): removed %d expired note(s)",
                     userroot.current() or "legacy", len(res["purged"]))
    finally:
        conn.close()


def _startup() -> None:
    """Init the DBs, then sweep any trash past the 30-day TTL."""
    for user in [None, *userroot.users()]:
        with userroot.bind(user):
            _purge_expired()


def _commit_user_store(user: str) -> None:
    """Record a user's flushed notes in their worktree; pin moved figures."""
    try:
        root = userroot.root_for(user)
        sha = autocommit.commit_subdir(root, "notes", user, "notes: autosave")
        if sha:
            log.info("notes: committed %s for %s", sha[:10], user)
        autocommit.pin_figures(root, user)
    except Exception:  # noqa: BLE001 — a commit failure must not stop the flush
        log.exception("notes: autocommit for %s failed", user)


def _flush_once() -> list[str]:
    """Persist every dirty room, per user, then commit each user's store.
    Opens its own connections so it is safe to run in a worker thread (a
    sqlite connection is bound to its creating thread)."""
    flushed = rooms.flush_everything(dao.connect)
    for user in flushed:
        if user:
            _commit_user_store(user)
    return [nid for ids in flushed.values() for nid in ids]


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
