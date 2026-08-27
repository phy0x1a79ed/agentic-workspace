"""Hub adapter for the trilium service — one knowledge base per person.

Registers with the gateway on the shared `ServiceAdapter` loop (register →
ready → serve → reconnect), so the fleet of Trilium servers is a service like
any other: visible in `awm services list`, health as a verb, and reachable from
any node rather than being node processes somebody started by hand.

Three things live under this one supervised process:

  - one Trilium server per user, on loopback — see `server`,
  - one TLS front per user, behind awm's edge session — see `front`,
  - the discovery that decides who "per user" means — see `instances`.

All of them die with this process. Trilium persists everything to its data
directory, so an awm deploy costs a browser reload rather than any content.

Run via `run.sh` (which the gateway spawns and respawns):
    python -m awm.trilium.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter, spawn_supervised

from awm.trilium import etapi, front, instances, server, vault

log = logging.getLogger("awm.trilium.hub_adapter")

FLEET = server.Fleet()

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
                "Report every Trilium instance on this node: which users have "
                "a scope, whether each server is running and at what pid/port, "
                "whether it is actually listening, which rolling backups that "
                "user has, each mesh front's TLS state with the URL to open, "
                "and which server bundle is being served — the fork worktree "
                "or the published tarball — with whether the build matches the "
                "revision on disk."
            ),
            "params": [],
        },
        {
            "name": "users",
            "tool": "trilium_users",
            "description": (
                "The users this node serves and the ports they own. A user "
                "exists because a scope userdata/trilium/<user> exists on "
                "disk, so adding a person is `awm scope create` and nothing "
                "else. Ports are allocated once and remembered, so adding a "
                "user never moves an existing one's URL."
            ),
            "params": [],
        },
        {
            "name": "start",
            "tool": "trilium_start",
            "description": (
                "Start a user's server, waiting until it binds. With no user, "
                "start every user's. Idempotent."
            ),
            "params": [
                {"name": "user", "type": "string",
                 "description": "User to start. Omit for all."},
            ],
            "timeout": 300,
        },
        {
            "name": "stop",
            "tool": "trilium_stop",
            "description": (
                "Stop a user's server, ending live browser connections. "
                "Content is on disk, so this loses no notes. With no user, "
                "stop every user's. Idempotent. The supervision loop respawns "
                "a stopped server on its next pass — use this to bounce one, "
                "not to keep it down."
            ),
            "params": [
                {"name": "user", "type": "string",
                 "description": "User to stop. Omit for all."},
            ],
            "timeout": 120,
        },
        {
            "name": "restart",
            "tool": "trilium_restart",
            "description": (
                "Stop a user's server and start it again — e.g. to pick up a "
                "rebuilt bundle or an edited config.ini, both read at startup."
            ),
            "params": [
                {"name": "user", "type": "string",
                 "description": "User to restart. Omit for all."},
            ],
            "timeout": 300,
        },
        {
            "name": "url",
            "tool": "trilium_url",
            "description": (
                "The mesh URL that opens a user's Trilium. Behind awm's edge "
                "session, and then behind that user's own Trilium login — two "
                "gates, because the edge session is one shared password and "
                "only the second one says which person."
            ),
            "params": [
                {"name": "user", "type": "string", "required": True,
                 "description": "Whose instance to link to."},
            ],
        },
        {
            "name": "authorize",
            "tool": "trilium_authorize",
            "description": (
                "Give this service an ETAPI token for a user, so snapshot and "
                "export can reach their vault. Preferred: the person creates a "
                "token in Trilium under Options -> ETAPI and passes it as "
                "`token`. Passing `password` instead exchanges it for a token "
                "over loopback and discards it — the password is never stored, "
                "but it does travel through this call. Either way the token is "
                "written 0600 in service state and is revocable from that same "
                "options screen. Pass `forget` to drop the stored token."
            ),
            "params": [
                {"name": "user", "type": "string", "required": True,
                 "description": "Whose vault to authorize against."},
                {"name": "token", "type": "string",
                 "description": "An ETAPI token created in Trilium's options."},
                {"name": "password", "type": "string",
                 "description": "That user's Trilium password, exchanged for a "
                                "token and not retained."},
                {"name": "forget", "type": "boolean",
                 "description": "Delete the stored token instead."},
            ],
        },
        {
            "name": "snapshot",
            "tool": "trilium_snapshot",
            "description": (
                "Take a named point this vault can be returned to. Without "
                "`note_id` that is the whole database: Trilium copies it under "
                "its sync mutex, the copy moves into the DVC chunk under a name "
                "carrying a UTC timestamp, and the pin is committed. With "
                "`note_id` it is one note's revision instead — Trilium's own "
                "machinery, restorable with one click in its revisions dialog."
            ),
            "params": [
                {"name": "user", "type": "string", "required": True,
                 "description": "Whose vault to snapshot."},
                {"name": "name", "type": "string",
                 "description": "Label for the snapshot. A UTC timestamp is "
                                "appended, so a name is never reused."},
                {"name": "note_id", "type": "string",
                 "description": "Snapshot one note as a revision instead of the "
                                "whole database."},
                {"name": "commit", "type": "boolean",
                 "description": "Pin and commit the result (default true)."},
            ],
            "timeout": 600,
        },
        {
            "name": "snapshots",
            "tool": "trilium_snapshots",
            "description": (
                "Every database copy a user has, newest first. `snapshot` "
                "entries are named, pinned and durable. `rolling` entries are "
                "Trilium's own daily/weekly/monthly rotation — overwritten on a "
                "schedule and pinned by nothing, so they are a race, not an "
                "archive."
            ),
            "params": [
                {"name": "user", "type": "string", "required": True,
                 "description": "Whose snapshots to list."},
            ],
        },
        {
            "name": "restore",
            "tool": "trilium_restore",
            "description": (
                "Replace a user's whole vault with a snapshot, stopping and "
                "restarting their server around the swap. Destructive: every "
                "note written since that snapshot is gone from the live vault. "
                "Nothing is deleted — the database being replaced is moved to "
                "live/superseded/<timestamp>/ and can be moved back. Requires "
                "`confirm`. Restoring a single note is one click in Trilium's "
                "own revisions dialog and is not this verb."
            ),
            "params": [
                {"name": "user", "type": "string", "required": True,
                 "description": "Whose vault to restore."},
                {"name": "snapshot", "type": "string", "required": True,
                 "description": "Snapshot name from `trilium snapshots`."},
                {"name": "confirm", "type": "boolean",
                 "description": "Must be true. Without it the verb reports what "
                                "it would replace and does nothing."},
            ],
            "timeout": 600,
        },
        {
            "name": "export",
            "tool": "trilium_export",
            "description": (
                "Export a user's vault as markdown into their scope's notes/ "
                "directory and commit it, pinning the snapshot chunk in the "
                "same commit. The markdown is a DERIVED VIEW: Trilium stores "
                "markup as HTML, so this is a conversion and importing it back "
                "is lossy. It is for reading, diffing, searching and merging by "
                "a person. Recovery is a snapshot, never this."
            ),
            "params": [
                {"name": "user", "type": "string", "required": True,
                 "description": "Whose vault to export."},
                {"name": "note_id", "type": "string",
                 "description": "Subtree to export (default the whole vault)."},
                {"name": "commit", "type": "boolean",
                 "description": "Commit the result (default true)."},
            ],
            "timeout": 600,
        },
        {
            "name": "logs",
            "tool": "trilium_logs",
            "description": "Tail one user's server stdout/stderr log.",
            "params": [
                {"name": "user", "type": "string", "required": True,
                 "description": "Whose log to read."},
                {"name": "tail", "type": "number",
                 "description": "Lines to return (default 200)."},
            ],
        },
    ],
    "emitters": [],
    "sessions": [],
}


# -- handlers ---------------------------------------------------------------
#
# Every handler hops to a worker thread: they spawn processes, signal groups
# and poll sockets, and any of those on the event loop would stall the control
# WS and have the gateway take this service for dead.


def _resolve(user: str | None) -> list[server.Child]:
    """The children a verb addresses. Named users must exist; absent means all."""
    if not user:
        return FLEET.children()
    child = FLEET.get(user)
    if child is None:
        known = ", ".join(sorted(c.inst.user for c in FLEET.children())) or "none"
        raise KeyError(f"no Trilium instance for user {user!r} (known: {known})")
    return [child]


async def _h_status(args: dict) -> dict:
    def _read() -> list[dict]:
        # Discovery first: a scope created a moment ago is a person who is
        # waiting for a URL, and making them wait for the next supervision pass
        # to be told they exist reads as the service having missed them.
        FLEET.sync()
        rows = FLEET.snapshot()
        for row in rows:
            inst = instances.instance(row.get("user", ""))
            # Counted here rather than in the supervisor: "this person has a
            # durable copy" is a fact about their scope, not about the process.
            # The rolling copies the supervisor reports are overwritten on a
            # schedule, so they are not the answer to that question.
            row["snapshots"] = (
                len([s for s in vault.snapshots(inst)["snapshots"]
                     if s["kind"] == "snapshot"]) if inst else 0)
            row["authorized"] = bool(inst and etapi.read_token(inst))
        return rows

    return {
        "instances": await asyncio.to_thread(_read),
        "fronts": front.status(),
        "source": await asyncio.to_thread(instances.source_state),
    }


async def _h_users(args: dict) -> dict:
    def _read() -> dict:
        FLEET.sync()
        return {
            "users": [
                {"user": i.user, "slot": i.slot,
                 "front_port": i.front_port, "upstream_port": i.upstream_port,
                 "scope": str(i.scope)}
                for i in instances.instances()
            ],
            "userdata_dir": str(instances.USERDATA_DIR),
            "max_users": instances.MAX_USERS,
        }
    return await asyncio.to_thread(_read)


async def _h_start(args: dict) -> dict:
    user = (args.get("user") or "").strip() or None
    if user is None:
        return {"results": await asyncio.to_thread(FLEET.start_all)}
    children = await asyncio.to_thread(_resolve, user)
    return {"results": [await asyncio.to_thread(c.start) for c in children]}


async def _h_stop(args: dict) -> dict:
    user = (args.get("user") or "").strip() or None
    children = await asyncio.to_thread(_resolve, user)
    return {"results": [await asyncio.to_thread(c.stop) for c in children]}


async def _h_restart(args: dict) -> dict:
    user = (args.get("user") or "").strip() or None
    children = await asyncio.to_thread(_resolve, user)
    return {"results": [await asyncio.to_thread(c.restart) for c in children]}


async def _h_url(args: dict) -> dict:
    user = (args.get("user") or "").strip()
    inst = await asyncio.to_thread(instances.instance, user)
    if inst is None:
        raise KeyError(f"no Trilium instance for user {user!r}")
    return {"user": user, "url": front.origin(inst),
            "note": "requires an awm session, then this user's Trilium login"}


async def _h_logs(args: dict) -> dict:
    user = (args.get("user") or "").strip()
    tail = int(args.get("tail") or 200)
    children = await asyncio.to_thread(_resolve, user)
    child = children[0]
    return {"user": user, "tail": tail,
            "log": await asyncio.to_thread(child.logs, tail)}


def _inst(args: dict) -> instances.Instance:
    user = (args.get("user") or "").strip()
    inst = instances.instance(user)
    if inst is None:
        known = ", ".join(instances.discovered_users()) or "none"
        raise KeyError(f"no Trilium instance for user {user!r} (known: {known})")
    return inst


async def _h_authorize(args: dict) -> dict:
    inst = await asyncio.to_thread(_inst, args)

    def _run() -> dict:
        if args.get("forget"):
            return {"user": inst.user, "forgotten": etapi.forget_token(inst)}
        token = (args.get("token") or "").strip()
        source = "supplied"
        if not token:
            password = args.get("password") or ""
            if not password:
                raise ValueError(
                    "pass either `token` (created in Trilium under Options -> "
                    "ETAPI) or `password` (exchanged for one and not stored)")
            token = etapi.login(inst, password, token_name=f"awm-{inst.user}")
            source = "issued"
        path = etapi.store_token(inst, token)
        # Prove the token works now rather than at the next snapshot, when the
        # failure would look like a broken backup instead of a bad credential.
        info = etapi.Etapi(inst, token).app_info()
        return {"user": inst.user, "token": source, "stored": str(path),
                "app_version": info.get("appVersion"),
                "db_version": info.get("dbVersion")}
    return await asyncio.to_thread(_run)


async def _h_snapshot(args: dict) -> dict:
    inst = await asyncio.to_thread(_inst, args)
    return await asyncio.to_thread(
        vault.snapshot, inst, (args.get("name") or "").strip() or None,
        note_id=(args.get("note_id") or "").strip() or None,
        commit=args.get("commit", True) is not False)


async def _h_snapshots(args: dict) -> dict:
    inst = await asyncio.to_thread(_inst, args)
    return await asyncio.to_thread(vault.snapshots, inst)


async def _h_restore(args: dict) -> dict:
    inst = await asyncio.to_thread(_inst, args)
    name = (args.get("snapshot") or "").strip()
    source = await asyncio.to_thread(vault.resolve_snapshot, inst, name)

    if not args.get("confirm"):
        return {
            "user": inst.user, "would_restore": str(source), "confirmed": False,
            "warning": (f"this replaces {inst.document_db} and every note "
                        f"written since that snapshot. Pass confirm=true."),
        }

    child = FLEET.get(inst.user)

    def _swap() -> dict:
        # `hold` keeps the supervision loop from respawning the child between
        # the stop and the swap. The start is in a `finally` because a failed
        # restore that also left the server down would be two problems, and the
        # second one has no message anywhere.
        stopped = child.stop(hold=True) if child else {"action": "no child"}
        try:
            report = vault.restore_files(inst, source)
        finally:
            started = child.start() if child else {"action": "no child"}
        report["stopped"] = stopped
        report["started"] = started
        return report
    return await asyncio.to_thread(_swap)


async def _h_export(args: dict) -> dict:
    inst = await asyncio.to_thread(_inst, args)
    return await asyncio.to_thread(
        vault.export, inst,
        note_id=(args.get("note_id") or "").strip() or "root",
        commit=args.get("commit", True) is not False)


HANDLERS = {
    "status": _h_status,
    "users": _h_users,
    "start": _h_start,
    "stop": _h_stop,
    "restart": _h_restart,
    "url": _h_url,
    "logs": _h_logs,
    "authorize": _h_authorize,
    "snapshot": _h_snapshot,
    "snapshots": _h_snapshots,
    "restore": _h_restore,
    "export": _h_export,
}


# -- the supervision loop ---------------------------------------------------


async def _health_loop() -> None:
    """Respawn dead children and raise fronts for new users. Never exits.

    Watches process liveness rather than an HTTP probe: a slow probe while
    Trilium is importing a large attachment is not evidence of death, and
    respawning on it would cut the work it was mistaking for a hang.
    """
    log.info("trilium: supervision loop started (interval=%ss)",
             instances.HEALTH_INTERVAL_S)
    while True:
        try:
            await asyncio.sleep(instances.HEALTH_INTERVAL_S)
            for res in await asyncio.to_thread(FLEET.reconcile):
                if res.get("action") == "respawned":
                    log.warning("trilium[%s]: respawned (previous exit %s)",
                                res.get("user"), res.get("previous_exit"))
            for user in await asyncio.to_thread(front.sync):
                log.info("trilium[%s]: front raised", user)
        except Exception:  # noqa: BLE001 — never let the loop die
            # CancelledError is a BaseException and so passes through, which is
            # what the supervisor above wants: a *return* from here would read
            # as a defect and be respawned, but a cancellation is a shutdown.
            log.exception("trilium: supervision pass failed")


async def _on_start() -> None:
    """Start every user's server, raise their fronts, then loop.

    No failure here is fatal. The service still registers, so `status` can
    report *why* it is broken, and the loop keeps retrying.
    """
    if instances.entry_point() is None:
        log.warning("trilium: no server bundle at %s or %s — run install.sh; "
                    "the service will register and report this via status",
                    instances.FORK_ENTRY, instances.TARBALL_ENTRY)
    else:
        try:
            for res in await asyncio.to_thread(FLEET.start_all):
                log.info("trilium[%s]: %s pid=%s listening=%s",
                         res.get("user"), res.get("action"), res.get("pid"),
                         res.get("listening"))
        except Exception:  # noqa: BLE001
            log.exception("trilium: initial start failed; the loop will retry")

    try:
        await asyncio.to_thread(front.sync)
    except Exception:  # noqa: BLE001 — the servers are still usable on loopback
        log.exception("trilium: mesh fronts failed to start")

    # A dead supervision loop looks exactly like a fleet that has not crashed,
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
