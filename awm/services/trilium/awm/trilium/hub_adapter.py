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
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter, spawn_supervised

from awm.trilium import etapi, instances, server, vault

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
            "parameters": [],
        },
        {
            "name": "start",
            "tool": "trilium_start",
            "description": (
                "Start the vault's server if it is not running. Operator only: "
                "the supervision loop already does this within seconds."
            ),
            "parameters": [],
            "timeout": 300,
        },
        {
            "name": "stop",
            "tool": "trilium_stop",
            "description": (
                "Stop the vault's server. Operator only — this takes the "
                "knowledge base away from everyone using it."
            ),
            "parameters": [],
            "timeout": 300,
        },
        {
            "name": "restart",
            "tool": "trilium_restart",
            "description": "Stop then start the vault's server. Operator only.",
            "parameters": [],
            "timeout": 300,
        },
        {
            "name": "url",
            "tool": "trilium_url",
            "description": (
                "Where the vault is served. A path, not a URL: it is on the "
                "same origin as the page asking, behind the same session."
            ),
            "parameters": [],
        },
        {
            "name": "provision",
            "tool": "trilium_provision",
            "description": (
                "Create the vault's database if it has none. Idempotent, and "
                "the supervision loop does it unprompted — this verb is for "
                "when it failed and you want the error. Operator only."
            ),
            "parameters": [],
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
            "parameters": [
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
            "parameters": [],
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
            "parameters": [
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
            "parameters": [
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
            "parameters": [
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
            "parameters": [
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
    if instances.entry_point() is None:
        log.warning("trilium: no server bundle at %s or %s — run install.sh; "
                    "the service will register and report this via status",
                    instances.FORK_ENTRY, instances.TARBALL_ENTRY)
    elif not instances.VAULT.exists:
        log.warning("trilium: no vault worktree at %s — create it with "
                    "`awm scope create --project vault --scope main`; the "
                    "service will register and report this via status",
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
