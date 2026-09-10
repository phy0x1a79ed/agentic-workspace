"""Hub adapter for the trilium service — one shared knowledge base.

Registers with the gateway on the shared `ServiceAdapter` loop (register →
ready → serve → reconnect), so the vault is a service like any other: visible
in `awm services list`, health as a verb, and reachable from any node rather
than a node process somebody started by hand.

Two things live under this one supervised process: the Trilium server on
loopback (see `server`), and the data lifecycle verbs that snapshot, restore
and export what is in it (see `vault`). There is no front here and no
discovery: the awm edge serves the vault at `/trilium/`, and there is one vault, so
there is nothing to enumerate.

**Who may call what.** The vault is shared, so a write verb is one person's
button acting on everyone's work. `restore` in particular replaces the whole
database. The split is enforced by `_operator_only` rather than by the public
edge's allow-list, because a mesh node's edge runs no allow-list at all — see
that function for the discriminator it uses.

Run via `run.sh` (which the gateway spawns and respawns):
    python -m awm.trilium.hub_adapter
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import time
import urllib.parse
from pathlib import Path
from typing import Any

from awm import config
from awm.gatewayclient import ServiceAdapter, spawn_supervised

from awm.trilium import etapi, instances, server, slices, vault

log = logging.getLogger("awm.trilium.hub_adapter")

CHILD = server.CHILD

#: Every function carries an explicit `tool` name under a `trilium_` prefix,
#: which is what decides the domain this service appears as: the gateway folds
#: the MCP surface by splitting the projected name on its **first** underscore.
#: So the surface is `awm trilium status` and `mcp__awm__trilium {verb:"status"}`.
API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "status",
            "tool": "trilium_status",
            "description": (
                "Whether the vault is up, whether it has a database yet, how "
                "many pinned snapshots it has, and which bundle is serving it. "
                "A caller arriving through the edge gets the readable half; "
                "pids, ports and absolute paths are for the console."
            ),
            "params": [],
        },
        {
            "name": "start",
            "tool": "trilium_start",
            "description": (
                "Start the vault's server if it is not running. Operator only: "
                "the supervision loop already does this within seconds."
            ),
            "params": [],
            "timeout": 300,
        },
        {
            "name": "stop",
            "tool": "trilium_stop",
            "description": (
                "Stop the vault's server. Operator only — this takes the "
                "knowledge base away from everyone using it."
            ),
            "params": [],
            "timeout": 300,
        },
        {
            "name": "restart",
            "tool": "trilium_restart",
            "description": "Stop then start the vault's server. Operator only.",
            "params": [],
            "timeout": 300,
        },
        {
            "name": "url",
            "tool": "trilium_url",
            "description": (
                "Where the vault is served. A path, not a URL: it is on the "
                "same origin as the page asking, behind the same session."
            ),
            "params": [],
        },
        {
            "name": "provision",
            "tool": "trilium_provision",
            "description": (
                "Create the vault's database if it has none. Idempotent, and "
                "the supervision loop does it unprompted — this verb is for "
                "when it failed and you want the error. Operator only."
            ),
            "params": [],
        },
        {
            "name": "snapshot",
            "tool": "trilium_snapshot",
            "description": (
                "Ask Trilium for a consistent database copy, move it into the "
                "DVC chunk under a name that is never reused, and commit the "
                "pin. With note_id, saves that note's revision instead. "
                "Operator only: it copies the whole database each time."
            ),
            "params": [
                {"name": "name", "type": "string",
                 "description": "Snapshot name. Defaults to a UTC timestamp."},
                {"name": "note_id", "type": "string",
                 "description": "Save a revision of this note instead of a database copy."},
                {"name": "commit", "type": "boolean",
                 "description": "Commit and pin the result. Default true."},
            ],
            "timeout": 600,
        },
        {
            "name": "snapshots",
            "tool": "trilium_snapshots",
            "description": (
                "Every database copy the vault has, newest first: the pinned "
                "snapshots and Trilium's own rolling rotation, kept apart "
                "because only the first kind is a restore path."
            ),
            "params": [],
        },
        {
            "name": "restore",
            "tool": "trilium_restore",
            "description": (
                "Replace the whole vault with a snapshot, moving the database "
                "it replaced into live/superseded/. Whole-vault, never one "
                "note — Trilium's own revisions dialog does that. Operator "
                "only, and it discards everyone's work since the snapshot."
            ),
            "params": [
                {"name": "snapshot", "type": "string", "required": True,
                 "description": "Snapshot name, from `trilium snapshots`."},
                {"name": "confirm", "type": "boolean",
                 "description": "Required. Without it this reports what it would do."},
            ],
            "timeout": 600,
        },
        {
            "name": "export",
            "tool": "trilium_export",
            "description": (
                "Export the vault as markdown into notes/ and commit it. A "
                "derived, lossy view for reading and diffing — recovery is a "
                "snapshot. Operator only: it rebuilds the whole tree."
            ),
            "params": [
                {"name": "note_id", "type": "string",
                 "description": "Subtree to export. Default the whole vault."},
                {"name": "commit", "type": "boolean",
                 "description": "Commit the result. Default true."},
            ],
            "timeout": 600,
        },
        {
            "name": "logs",
            "tool": "trilium_logs",
            "description": (
                "Tail the vault server's log. Operator only: it carries "
                "absolute paths and stack traces."
            ),
            "params": [
                {"name": "tail", "type": "number",
                 "description": "Lines from the end. Default 200."},
            ],
        },
        {
            "name": "note_upsert",
            "tool": "trilium_note_upsert",
            "description": (
                "Write a note: create it under a parent, or replace the body "
                "of the one already there with that exact title. Operator "
                "only, and it refuses a title that matches twice rather than "
                "overwrite the wrong note on a vault everybody shares."
            ),
            "params": [
                {"name": "title", "type": "string", "required": True,
                 "description": "Matched exactly against the parent's children."},
                {"name": "content", "type": "string", "required": True,
                 "description": "The note's body. HTML for a text note."},
                {"name": "parent", "type": "string",
                 "description": "Parent note id. Default root."},
                {"name": "type", "type": "string",
                 "description": "Trilium note type. Default text."},
            ],
        },
        {
            "name": "note_get",
            "tool": "trilium_note_get",
            "description": (
                "One note: its title, type, attributes, attachments, parents "
                "and children, and its body unless you ask for it left out. "
                "Operator only, like every note verb — see note_upsert."
            ),
            "params": [
                {"name": "note_id", "type": "string", "required": True,
                 "description": "Note id. `root` is the top of the tree."},
                {"name": "content", "type": "boolean",
                 "description": "Include the body. Default true."},
            ],
        },
        {
            "name": "note_children",
            "tool": "trilium_note_children",
            "description": (
                "The direct children of a note, in tree order, without their "
                "bodies. Operator only."
            ),
            "params": [
                {"name": "note_id", "type": "string",
                 "description": "Parent note id. Default root."},
            ],
        },
        {
            "name": "note_search",
            "tool": "trilium_note_search",
            "description": (
                "Search the vault in Trilium's own search grammar — "
                "`#status=Doing`, `note.title *=* paper`, plain words. "
                "Operator only."
            ),
            "params": [
                {"name": "query", "type": "string", "required": True,
                 "description": "A Trilium search expression."},
                {"name": "ancestor", "type": "string",
                 "description": "Restrict to this note's subtree."},
                {"name": "limit", "type": "number",
                 "description": "Maximum hits. Default 50."},
                {"name": "fast", "type": "boolean",
                 "description": "Skip note bodies. Default true."},
                {"name": "archived", "type": "boolean",
                 "description": "Include archived notes. Default false, "
                                "which is Trilium's own."},
            ],
        },
        {
            "name": "note_create",
            "tool": "trilium_note_create",
            "description": (
                "Create a note under a parent and return its id. Unlike "
                "note_upsert this always makes a new one, so it is what to "
                "call when the title is not the identity. Attributes given "
                "here are set in the same call, which ETAPI itself cannot do. "
                "Operator only."
            ),
            "params": [
                {"name": "title", "type": "string", "required": True,
                 "description": "The note's title."},
                {"name": "content", "type": "string",
                 "description": "The body. HTML for a text note. Default empty."},
                {"name": "parent", "type": "string",
                 "description": "Parent note id. Default root."},
                {"name": "type", "type": "string",
                 "description": "Trilium note type. Default text."},
                {"name": "mime", "type": "string",
                 "description": "MIME type, for a code or file note."},
                {"name": "labels", "type": "object",
                 "description": "Labels to set, as {name: value}."},
                {"name": "relations", "type": "object",
                 "description": "Relations to set, as {name: target note id}."},
            ],
        },
        {
            "name": "note_update",
            "tool": "trilium_note_update",
            "description": (
                "Change a note in place: its title, type, mime, body, or any "
                "of them at once. Only what you name is touched. Operator only."
            ),
            "params": [
                {"name": "note_id", "type": "string", "required": True,
                 "description": "The note to change."},
                {"name": "title", "type": "string",
                 "description": "New title."},
                {"name": "content", "type": "string",
                 "description": "New body. Replaces the old one entirely."},
                {"name": "type", "type": "string",
                 "description": "New Trilium note type."},
                {"name": "mime", "type": "string",
                 "description": "New MIME type."},
                {"name": "labels", "type": "object",
                 "description": "Labels to set in the same call, as {name: value}."},
            ],
        },
        {
            "name": "note_place",
            "tool": "trilium_note_place",
            "description": (
                "Make a note's parents exactly this set — one call for what "
                "would otherwise be a move, several clones and an unplace. "
                "The note is shown under each and nowhere else; it is one "
                "note in several places, never a copy. Operator only."
            ),
            "params": [
                {"name": "note_id", "type": "string", "required": True,
                 "description": "The note to place."},
                {"name": "parents", "type": "array", "required": True,
                 "description": "Every parent it should appear under."},
            ],
        },
        {
            "name": "note_delete",
            "tool": "trilium_note_delete",
            "description": (
                "Delete a note and everything under it. Operator only, and it "
                "refuses `root`: there is no undo here, only a snapshot."
            ),
            "params": [
                {"name": "note_id", "type": "string", "required": True,
                 "description": "The note to delete, with its subtree."},
            ],
        },
        {
            "name": "note_move",
            "tool": "trilium_note_move",
            "description": (
                "Move a note under a new parent, leaving it in one place. "
                "Operator only."
            ),
            "params": [
                {"name": "note_id", "type": "string", "required": True,
                 "description": "The note to move."},
                {"name": "parent", "type": "string", "required": True,
                 "description": "Its new parent."},
            ],
        },
        {
            "name": "note_clone",
            "tool": "trilium_note_clone",
            "description": (
                "Also show this note under another parent. One note in two "
                "places, not a copy — editing either edits the note. Operator "
                "only."
            ),
            "params": [
                {"name": "note_id", "type": "string", "required": True,
                 "description": "The note to place again."},
                {"name": "parent", "type": "string", "required": True,
                 "description": "The additional parent."},
            ],
        },
        {
            "name": "attrs_get",
            "tool": "trilium_attrs_get",
            "description": (
                "Every label and relation on a note, including the ones it "
                "inherits and which note owns each. Operator only."
            ),
            "params": [
                {"name": "note_id", "type": "string", "required": True,
                 "description": "The note to read."},
            ],
        },
        {
            "name": "attr_set",
            "tool": "trilium_attr_set",
            "description": (
                "Give a note exactly one label or relation of this name. "
                "Matched against the note's own attributes, never an "
                "inherited one — patching that would change every note "
                "sharing the template. Operator only."
            ),
            "params": [
                {"name": "note_id", "type": "string", "required": True,
                 "description": "The note to label."},
                {"name": "name", "type": "string", "required": True,
                 "description": "Attribute name, without the # or ~."},
                {"name": "value", "type": "string",
                 "description": "Label value, or the target note id for a relation."},
                {"name": "type", "type": "string",
                 "description": "label or relation. Default label."},
                {"name": "inheritable", "type": "boolean",
                 "description": "Also apply to the subtree. Default false."},
            ],
        },
        {
            "name": "attr_delete",
            "tool": "trilium_attr_delete",
            "description": (
                "Remove every attribute of this name the note owns. An "
                "inherited one is not the note's to remove and stays. "
                "Operator only."
            ),
            "params": [
                {"name": "note_id", "type": "string", "required": True,
                 "description": "The note to clear."},
                {"name": "name", "type": "string", "required": True,
                 "description": "Attribute name, without the # or ~."},
                {"name": "type", "type": "string",
                 "description": "label or relation. Default label."},
            ],
        },
        {
            "name": "attachment_put",
            "tool": "trilium_attachment_put",
            "description": (
                "Attach a file to a note, from a path on this host or from "
                "base64. Replaces an attachment of the same title, and skips "
                "the upload when the size already matches. Operator only: it "
                "reads a path the service can reach."
            ),
            "params": [
                {"name": "note_id", "type": "string", "required": True,
                 "description": "The note to attach to."},
                {"name": "path", "type": "string",
                 "description": "A file on this host. Either this or content_b64."},
                {"name": "content_b64", "type": "string",
                 "description": "The bytes, base64-encoded."},
                {"name": "title", "type": "string",
                 "description": "Attachment title. Defaults to the file's name."},
                {"name": "mime", "type": "string",
                 "description": "MIME type. Guessed from the name when omitted."},
            ],
            "timeout": 300,
        },
        {
            "name": "slice_expose",
            "tool": "trilium_slice_expose",
            "description": (
                "Mint a link that opens one note and its descendants to "
                "somebody with no awm account. Sets #sliced on the note so a "
                "person browsing the vault can see what is exposed. With "
                "user, the visitor's name is baked into the token (a bound "
                "slice); without it, anyone with the link types their own "
                "name on first arrival (an open slice). Prints the URL. "
                "Operator only."
            ),
            "params": [
                {"name": "note_id", "type": "string", "required": True,
                 "description": "The note the slice opens, with its subtree."},
                {"name": "user", "type": "string",
                 "description": "Bind the token to this visitor name. Omit for an open link."},
                {"name": "write", "type": "boolean",
                 "description": "Let the visitor edit note bodies. Default false (read-only)."},
                {"name": "expires_in_hours", "type": "number",
                 "description": "The link stops resolving after this many hours. Default never."},
            ],
        },
        {
            "name": "slice_list",
            "tool": "trilium_slice_list",
            "description": (
                "Every slice ever minted, newest first -- active and revoked "
                "alike. Operator only."
            ),
            "params": [
                {"name": "note_id", "type": "string",
                 "description": "Restrict to slices on this note. Default every slice."},
            ],
        },
        {
            "name": "slice_revoke",
            "tool": "trilium_slice_revoke",
            "description": (
                "Revoke a slice token; a later slice_resolve of it answers "
                "'no such slice'. Clears #sliced from the note once no other "
                "live slice remains on it. Operator only."
            ),
            "params": [
                {"name": "token", "type": "string", "required": True,
                 "description": "The token to revoke, from slice_expose or slice_list."},
            ],
        },
        {
            "name": "slice_resolve",
            "tool": "trilium_slice_resolve",
            "description": (
                "What a slice token opens: the note, whether the visitor may "
                "write, and the visitor name if the token is bound to one. An "
                "unknown, revoked or expired token answers the same 'not "
                "found' shape as every other -- the edge turns that into a "
                "404, deliberately indistinguishable from a link that never "
                "existed. Operator-only in mechanism: the edge is the caller, "
                "reaching this over the gateway on loopback with no identity "
                "header, which is exactly what the gate admits."
            ),
            "params": [
                {"name": "token", "type": "string", "required": True,
                 "description": "The token from the slice's URL path."},
            ],
        },
    ],
    "emitters": [],
    "sessions": [],
}


# -- who may call what ------------------------------------------------------


def _operator_only(as_: str | None, verb: str) -> None:
    """Refuse a verb that arrived through an edge listener.

    The vault is shared, so every write verb is one person acting on everyone's
    work, and `restore` discards it. Those belong to whoever can reach the host,
    not to whoever can reach the page.

    The discriminator needs no new credential because the edge already supplies
    one. `httpsfront` overwrites `X-Awm-As` on every request it forwards — the
    browser's own value is discarded — and it never forwards an empty one. So an
    absent identity here means the call did not cross an edge: it came from
    `/invoke` on loopback, which is the host's own CLI.

    This is deliberately *not* `userroot.wrap_handlers`. That answers "whose
    store?", which a shared vault never asks, and under `AWM_USER_ROOT_STRICT`
    it raises for exactly the caller we want to admit.
    """
    if as_ is not None:
        raise PermissionError(
            f"{verb} acts on the shared vault and is an operator verb: "
            f"run `awm trilium {verb}` on the host")


# -- handlers ---------------------------------------------------------------
#
# Every handler hops to a worker thread: they spawn processes, signal groups
# and poll sockets, and any of those on the event loop would stall the control
# WS and have the gateway take this service for dead.


async def _h_status(args: dict, as_: str | None = None) -> dict:
    verbose = as_ is None

    def _read() -> dict:
        state = CHILD.snapshot(verbose=verbose)
        # Counted here rather than in the supervisor: "there is a durable copy"
        # is a fact about the scope, not about the process. The rolling copies
        # the supervisor reports are overwritten on a schedule, so they are not
        # the answer to that question.
        try:
            state["snapshots"] = len(
                [s for s in vault.snapshots(instances.VAULT)["snapshots"]
                 if s["kind"] == "snapshot"])
        except OSError:
            state["snapshots"] = 0
        return state

    out = {"vault": await asyncio.to_thread(_read)}
    if verbose:
        out["source"] = await asyncio.to_thread(instances.source_state)
    return out


async def _h_start(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "start")
    return await asyncio.to_thread(CHILD.start)


async def _h_stop(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "stop")
    return await asyncio.to_thread(CHILD.stop)


async def _h_restart(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "restart")
    return await asyncio.to_thread(CHILD.restart)


async def _h_provision(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "provision")
    return await asyncio.to_thread(CHILD.provision)


async def _h_url(args: dict, as_: str | None = None) -> dict:
    """The vault's path, not a URL.

    There is nothing to compute: the vault is on the same origin as whatever
    page is rendering the link, reached through the same session. A host and a
    port here would be a guess, and the old one guessed wrong on any node with
    more than one address.
    """
    return {"path": "/trilium/"}


async def _h_logs(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "logs")
    tail = int(args.get("tail") or 200)
    return {"tail": tail, "log": await asyncio.to_thread(CHILD.logs, tail)}


async def _h_snapshot(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "snapshot")
    return await asyncio.to_thread(
        vault.snapshot, instances.VAULT, (args.get("name") or "").strip() or None,
        note_id=(args.get("note_id") or "").strip() or None,
        commit=args.get("commit", True) is not False)


async def _h_snapshots(args: dict, as_: str | None = None) -> dict:
    return await asyncio.to_thread(vault.snapshots, instances.VAULT)


async def _h_restore(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "restore")
    v = instances.VAULT
    name = (args.get("snapshot") or "").strip()
    source = await asyncio.to_thread(vault.resolve_snapshot, v, name)

    if not args.get("confirm"):
        return {
            "would_restore": str(source), "confirmed": False,
            "warning": (f"this replaces {v.document_db} and every note anyone "
                        f"has written since that snapshot. Pass confirm=true."),
        }

    def _swap() -> dict:
        # `hold` keeps the supervision loop from respawning the child between
        # the stop and the swap. The start is in a `finally` because a failed
        # restore that also left the server down would be two problems, and the
        # second one has no message anywhere.
        stopped = CHILD.stop(hold=True)
        try:
            report = vault.restore_files(v, source)
        finally:
            started = CHILD.start()
        report["stopped"] = stopped
        report["started"] = started
        return report
    return await asyncio.to_thread(_swap)


async def _h_export(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "export")
    return await asyncio.to_thread(
        vault.export, instances.VAULT,
        note_id=(args.get("note_id") or "").strip() or "root",
        commit=args.get("commit", True) is not False)


async def _h_note_upsert(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "note_upsert")
    title = (args.get("title") or "").strip()
    if not title:
        raise ValueError("title is required, and is matched exactly")
    content = args.get("content")
    if content is None:
        raise ValueError("content is required")
    return await asyncio.to_thread(
        etapi.client().upsert_note,
        parent_note_id=(args.get("parent") or "").strip() or "root",
        title=title, content=str(content),
        type=(args.get("type") or "").strip() or "text")


# -- the note API -----------------------------------------------------------
#
# Every verb here is operator-only, reads included. The edge does not forward
# `/etapi/`, and these verbs are the same surface by another name: a note in
# the vault can run script in the vault's origin, and an open read verb would
# let it walk a knowledge base its author was never shown. The person reading
# the vault already has all of it in front of them, so nothing is lost.


def _mapping(args: dict, key: str) -> dict[str, str]:
    """A `{name: value}` argument, however the surface it arrived on spells it.

    One catalog projects each verb onto MCP, HTTP and the CLI, and the CLI has
    no object type — `_json_schema_py_type` maps `object` to `str`, so
    `--labels '{"status":"Doing"}'` arrives as text while the same call over
    MCP arrives as a dict. Accepting both is what keeps `awm trilium
    note-create` and `mcp__awm__trilium` the same verb.
    """
    raw = args.get(key)
    if raw in (None, "", {}):
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"{key} is an object spelled as JSON, e.g. "
                f"""--{key} '{{"status": "To do"}}' — got {raw!r} ({e})"""
            ) from e
    if not isinstance(raw, dict):
        raise ValueError(f"{key} is an object of {{name: value}}, not {type(raw).__name__}")
    return {str(k): str(v) for k, v in raw.items()}


def _note_id(args: dict, key: str = "note_id", default: str | None = None) -> str:
    got = (args.get(key) or "").strip() or default
    if not got:
        raise ValueError(f"{key} is required")
    return got


async def _h_note_get(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "note_get")
    note_id = _note_id(args)
    want_content = args.get("content", True) is not False

    def _read() -> dict:
        c = etapi.client()
        out = dict(c.note(note_id))
        out["attachments"] = c.attachments(note_id)
        if want_content:
            out["content"] = c.note_content(note_id)
        return out
    return await asyncio.to_thread(_read)


async def _h_note_children(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "note_children")
    note_id = _note_id(args, default="root")

    def _read() -> dict:
        kids = etapi.client().children(note_id)
        return {"parent": note_id, "count": len(kids), "children": [
            {"note_id": k.get("noteId"), "title": k.get("title"),
             "type": k.get("type"),
             "child_count": len(k.get("childNoteIds") or [])}
            for k in kids]}
    return await asyncio.to_thread(_read)


async def _h_note_search(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "note_search")
    query = (args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    params: dict[str, Any] = {
        "limit": int(args.get("limit") or 50),
        "fastSearch": args.get("fast", True) is not False,
    }
    ancestor = (args.get("ancestor") or "").strip()
    if ancestor:
        params["ancestorNoteId"] = ancestor
    # Trilium's search context appends "not archived" to every expression, and
    # the flag is inherited — so one archived ancestor hides a whole subtree
    # from a caller that owns notes inside it. Off by default, as upstream.
    if args.get("archived") is True:
        params["includeArchivedNotes"] = True
    return await asyncio.to_thread(etapi.client().search, query, **params)


async def _h_note_create(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "note_create")
    title = (args.get("title") or "").strip()
    if not title:
        raise ValueError("title is required")
    labels = _mapping(args, "labels")
    relations = _mapping(args, "relations")

    def _write() -> dict:
        c = etapi.client()
        made = c.create_note(
            parent_note_id=(args.get("parent") or "").strip() or "root",
            title=title, type=(args.get("type") or "").strip() or "text",
            content=str(args.get("content") or ""),
            mime=(args.get("mime") or "").strip() or None)
        note_id = made["note"]["noteId"]
        # A second call each, because ETAPI's create-note whitelist takes no
        # attributes. Doing it here is the point of the verb: every caller
        # would otherwise write this loop, and half would forget the relation.
        c.set_attributes(note_id=note_id, values={
            name.lstrip("#"): value for name, value in labels.items()})
        c.set_attributes(note_id=note_id, type="relation", values={
            name.lstrip("~"): target for name, target in relations.items()})
        return {"note_id": note_id, "created": True,
                "labels": len(labels), "relations": len(relations)}
    return await asyncio.to_thread(_write)


async def _h_note_update(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "note_update")
    note_id = _note_id(args)
    fields = {k: args[k] for k in ("title", "type", "mime")
              if args.get(k) not in (None, "")}
    content = args.get("content")
    labels = _mapping(args, "labels")
    if not fields and content is None and not labels:
        raise ValueError(
            "nothing to change: give a title, content, type, mime or labels")

    def _write() -> dict:
        c = etapi.client()
        # One read serves the whole call: the field comparison below and the
        # attributes the batch needs. `changed` must report what actually
        # moved, not what was offered — a mirror running on a timer decides
        # from it whether it has work to do, and a blind `True` makes every
        # pass look like a change and writes a revision per note.
        note = c.note(note_id)
        differs = {k: v for k, v in fields.items() if note.get(k) != v}
        if differs:
            c.patch_note(note_id, **differs)
        changed: dict[str, Any] = dict(differs)
        if content is not None:
            if c.note_content(note_id) != str(content):
                c.set_content(note_id, str(content))
                changed["content"] = True
            else:
                changed["content"] = False
        # Set here rather than by a call each, because a sync writing five
        # fields onto a thousand notes is five thousand round trips otherwise.
        written = c.set_attributes(
            note_id=note_id,
            values={n.lstrip("#"): v for n, v in labels.items()},
            attributes=note.get("attributes") or [])
        if written:
            changed["labels"] = written
        return {"note_id": note_id, "changed": changed}
    return await asyncio.to_thread(_write)


async def _h_note_place(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "note_place")
    note_id = _note_id(args)
    parents = args.get("parents")
    if isinstance(parents, str):
        parents = [p.strip() for p in parents.split(",") if p.strip()]
    wanted = {str(p).strip() for p in (parents or []) if str(p).strip()}
    if not wanted:
        raise ValueError(
            "parents is required and may not be empty: a note with no branch "
            "is a deleted note, and note_delete at least says so")

    def _write() -> dict:
        c = etapi.client()
        current = set(c.note(note_id).get("parentNoteIds") or [])
        # Added before removed, always: a note's last branch takes the note
        # with it, so unplacing first would delete what this is placing.
        for parent in sorted(wanted - current):
            c.put_branch(note_id, parent)
        for parent in sorted(current - wanted):
            c.delete_branch(c.branch_id(note_id, parent))
        return {"note_id": note_id, "parents": sorted(wanted),
                "added": sorted(wanted - current),
                "removed": sorted(current - wanted)}
    return await asyncio.to_thread(_write)


async def _h_note_delete(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "note_delete")
    note_id = _note_id(args)
    if note_id == "root":
        raise ValueError(
            "root is the vault. Deleting it is `restore` from a snapshot, "
            "which at least says so.")

    def _write() -> dict:
        etapi.client().delete_note(note_id)
        return {"note_id": note_id, "deleted": True}
    return await asyncio.to_thread(_write)


async def _h_note_move(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "note_move")
    return await asyncio.to_thread(
        etapi.client().move_note, _note_id(args), _note_id(args, "parent"))


async def _h_note_clone(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "note_clone")
    note_id, parent = _note_id(args), _note_id(args, "parent")

    def _write() -> dict:
        made = etapi.client().put_branch(note_id, parent)
        return {"note_id": note_id, "parent": parent,
                "branch_id": made.get("branchId")}
    return await asyncio.to_thread(_write)


async def _h_attrs_get(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "attrs_get")
    note_id = _note_id(args)

    def _read() -> dict:
        attrs = etapi.client().attributes(note_id)
        return {"note_id": note_id, "attributes": [
            {"attribute_id": a.get("attributeId"), "type": a.get("type"),
             "name": a.get("name"), "value": a.get("value"),
             "inheritable": bool(a.get("isInheritable")),
             "owned": a.get("noteId") == note_id}
            for a in attrs]}
    return await asyncio.to_thread(_read)


async def _h_attr_set(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "attr_set")
    note_id = _note_id(args)
    name = (args.get("name") or "").strip().lstrip("#~")
    if not name:
        raise ValueError("name is required")
    return await asyncio.to_thread(
        etapi.client().set_attribute, note_id=note_id, name=name,
        value=str(args.get("value") or ""),
        type=(args.get("type") or "").strip() or "label",
        is_inheritable=bool(args.get("inheritable")))


async def _h_attr_delete(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "attr_delete")
    note_id = _note_id(args)
    name = (args.get("name") or "").strip().lstrip("#~")
    if not name:
        raise ValueError("name is required")

    def _write() -> dict:
        gone = etapi.client().clear_attribute(
            note_id=note_id, name=name,
            type=(args.get("type") or "").strip() or "label")
        return {"note_id": note_id, "name": name, "removed": gone}
    return await asyncio.to_thread(_write)


async def _h_attachment_put(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "attachment_put")
    note_id = _note_id(args)
    path = (args.get("path") or "").strip()
    b64 = args.get("content_b64")
    if bool(path) == bool(b64):
        raise ValueError("give exactly one of path or content_b64")

    def _write() -> dict:
        if path:
            src = Path(path).expanduser()
            blob = src.read_bytes()
            title = (args.get("title") or "").strip() or src.name
        else:
            blob = base64.b64decode(str(b64))
            title = (args.get("title") or "").strip()
            if not title:
                raise ValueError("title is required when the bytes come inline")
        mime = ((args.get("mime") or "").strip()
                or mimetypes.guess_type(title)[0]
                or "application/octet-stream")
        out = etapi.client().upsert_attachment(
            owner_id=note_id, title=title, mime=mime, blob=blob)
        out.update({"note_id": note_id, "title": title, "mime": mime,
                    "bytes": len(blob)})
        return out
    return await asyncio.to_thread(_write)


# -- slices -------------------------------------------------------------
#
# A slice opens one note and its descendants to somebody with no awm account.
# The token is a credential, so it lives in this service's own DB (see
# `slices.py`) rather than on a note. All four verbs are operator-only, and
# `slice_resolve` is the exception in spirit but not in mechanism: the edge
# calls it over the gateway on loopback, which carries no identity header --
# exactly what `_operator_only` admits.


def _slice_url(token: str, note_id: str, user: str | None) -> str:
    """The URL `slice_expose` prints: the token in the path, so the SPA's own
    relative references resolve inside the slice's mount and two slices open
    in one browser cannot collide on a cookie. `user` rides the query string
    only for a bound token, for transparency -- an open token carries none,
    and the visitor types a name on arrival."""
    edge = (config.edge_url() or "").rstrip("/")
    query = f"?user={urllib.parse.quote(user, safe='')}" if user else ""
    return f"{edge}/slice/{token}/{query}#root/{note_id}"


def _fork_only(verb: str) -> None:
    """Refuse a slice verb on a node serving the published tarball.

    The mask that confines a slice to one subtree lives in the fork. Upstream's
    build ignores the edge's slice headers entirely and answers every route, so a
    token resolved against it would open the whole vault to whoever holds the
    link. Refused at both ends -- minting and resolving -- because a database
    that reaches such a node by sync carries tokens minted elsewhere.
    """
    entry = instances.entry_point()
    if entry is not None and entry[1] != "fork":
        raise PermissionError(
            f"{verb} needs the Trilium fork: this node serves the published "
            f"tarball, which has no slice mask and would expose the whole vault")


async def _h_slice_expose(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "slice_expose")
    _fork_only("slice_expose")
    note_id = _note_id(args)
    user = (args.get("user") or "").strip() or None
    write = bool(args.get("write"))
    expires_in_hours = args.get("expires_in_hours")
    expires_at = (time.time() + float(expires_in_hours) * 3600
                  if expires_in_hours not in (None, "") else None)

    def _write() -> dict:
        # Set before the mint: a token that failed to record is no exposure
        # at all, but a note marked #sliced with no live token is merely
        # confusing, not a security hole.
        etapi.client().set_attribute(note_id=note_id, name="sliced", value="")
        return slices.mint(note_id=note_id, user=user, write=write,
                           expires_at=expires_at)
    row = await asyncio.to_thread(_write)
    return {**row, "url": _slice_url(row["token"], note_id, user)}


async def _h_slice_list(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "slice_list")
    note_id = (args.get("note_id") or "").strip() or None
    rows = await asyncio.to_thread(slices.list_all, note_id)
    # The url the same way `slice_expose` builds it, so a caller listing what is
    # already shared can hand a link over without minting a second one.
    return {"slices": [{**r, "url": _slice_url(r["token"], r["note_id"], r["user"])}
                       for r in rows]}


async def _h_slice_revoke(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "slice_revoke")
    token = (args.get("token") or "").strip()
    if not token:
        raise ValueError("token is required")

    def _write() -> dict:
        row = slices.revoke(token)
        if row is None:
            raise ValueError(f"no slice for token {token!r}")
        cleared = False
        if not slices.list_active(row["note_id"]):
            etapi.client().clear_attribute(note_id=row["note_id"], name="sliced")
            cleared = True
        return {"token": token, "note_id": row["note_id"], "revoked": True,
                "sliced_cleared": cleared}
    return await asyncio.to_thread(_write)


async def _h_slice_resolve(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "slice_resolve")
    _fork_only("slice_resolve")
    token = (args.get("token") or "").strip()
    row = await asyncio.to_thread(slices.resolve, token) if token else None
    if row is None:
        return {"found": False, "note_id": None, "write": False, "user": None}
    return {"found": True, "note_id": row["note_id"], "write": row["write"],
            "user": row["user"]}


HANDLERS = {
    "status": _h_status,
    "start": _h_start,
    "stop": _h_stop,
    "restart": _h_restart,
    "url": _h_url,
    "provision": _h_provision,
    "logs": _h_logs,
    "snapshot": _h_snapshot,
    "snapshots": _h_snapshots,
    "restore": _h_restore,
    "export": _h_export,
    "note_upsert": _h_note_upsert,
    "note_get": _h_note_get,
    "note_children": _h_note_children,
    "note_search": _h_note_search,
    "note_create": _h_note_create,
    "note_update": _h_note_update,
    "note_delete": _h_note_delete,
    "note_move": _h_note_move,
    "note_place": _h_note_place,
    "note_clone": _h_note_clone,
    "attrs_get": _h_attrs_get,
    "attr_set": _h_attr_set,
    "attr_delete": _h_attr_delete,
    "attachment_put": _h_attachment_put,
    "slice_expose": _h_slice_expose,
    "slice_list": _h_slice_list,
    "slice_revoke": _h_slice_revoke,
    "slice_resolve": _h_slice_resolve,
}


# -- the supervision loop ---------------------------------------------------


async def _health_loop() -> None:
    """Respawn the child if it died, and provision it if it has no database.
    Never exits.

    Watches process liveness rather than an HTTP probe: a slow probe while
    Trilium is importing a large attachment is not evidence of death, and
    respawning on it would cut the work it was mistaking for a hang.
    """
    log.info("trilium: supervision loop started (interval=%ss)",
             instances.HEALTH_INTERVAL_S)
    while True:
        try:
            await asyncio.sleep(instances.HEALTH_INTERVAL_S)
            res = await asyncio.to_thread(CHILD.reconcile)
            if res.get("action") == "respawned":
                log.warning("trilium: respawned (previous exit %s)",
                            res.get("previous_exit"))
            elif res.get("action") == "respawn-failed":
                log.error("trilium: respawn failed: %s", res.get("error"))
        except Exception:  # noqa: BLE001 — never let the loop die
            # CancelledError is a BaseException and so passes through, which is
            # what the supervisor above wants: a *return* from here would read
            # as a defect and be respawned, but a cancellation is a shutdown.
            log.exception("trilium: supervision pass failed")


async def _on_start() -> None:
    """Start the vault's server, give it a database if it has none, then loop.

    No failure here is fatal. The service still registers, so `status` can
    report *why* it is broken, and the loop keeps retrying.
    """
    slices.init()
    if instances.entry_point() is None:
        log.warning("trilium: no server bundle at %s or %s — run install.sh; "
                    "the service will register and report this via status",
                    instances.FORK_ENTRY, instances.TARBALL_ENTRY)
    elif not instances.VAULT.exists:
        log.warning("trilium: no vault directory at %s — run "
                    "awm/services/trilium/install.sh; the service will "
                    "register and report this via status",
                    instances.VAULT.scope)
    else:
        try:
            res = await asyncio.to_thread(CHILD.start)
            log.info("trilium: %s pid=%s listening=%s initialized=%s",
                     res.get("action"), res.get("pid"), res.get("listening"),
                     res.get("initialized"))
        except Exception:  # noqa: BLE001
            log.exception("trilium: initial start failed; the loop will retry")

    # A dead supervision loop looks exactly like a vault that has not crashed,
    # so it is spawned supervised rather than as a bare task nobody reads.
    spawn_supervised("trilium:health", _health_loop)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter("trilium", API_MANIFEST, HANDLERS,
                         on_start=_on_start).run()


if __name__ == "__main__":
    asyncio.run(main())
