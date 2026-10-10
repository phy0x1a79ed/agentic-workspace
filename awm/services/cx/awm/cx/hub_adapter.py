"""Hub adapter for the `cx` service — Claude Code sessions on this node.

`start`, `list` and `stop` are the session lifecycle other agents use. The rest
of the service is the warm start: it keeps one background Claude Code session
idling so that `cx` in a terminal attaches to a renderer that has already
started, instead of paying a cold launch. The session is seeded in a neutral
directory and moved to the caller's directory at claim time, so the pool is not
keyed by directory and the first `cx` in a project is as fast as the hundredth.

Every function carries an explicit ``tool`` name under a ``cx_`` prefix, which
is what decides the domain: the gateway folds the MCP surface by splitting the
projected name on its first underscore. So the surface is ``awm cx list`` and
``mcp__awm__cx {verb:"list"}``.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.cx.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter, spawn_supervised

from awm.cx import claim, lifecycle, pool, reconcile, remove, seed

log = logging.getLogger("awm.cx.hub_adapter")

LOOP = reconcile.Loop()

API_MANIFEST: dict[str, Any] = {
    "description": (
        "Claude Code sessions on this node. `start` creates a named background "
        "session in a scope's worktree, `list` shows every session with its "
        "lineage, and `stop` ends one that `start` created. The service also "
        "keeps one warm session idling so the `cx` command in a terminal "
        "attaches to an already-painted renderer; `claim`, `seed` and `remove` "
        "operate that pool, and `cx` itself is the everyday surface."
    ),
    "functions": [
        {
            "name": "start",
            "tool": "cx_start",
            "description": (
                "Start a named background Claude Code session in a scope's "
                "worktree, creating the worktree if it is absent, and return "
                "its job id and the `claude attach` command. Refuses on a "
                "station, for a caller that is not a domestic peer, when no "
                "Claude Code daemon runs, when the worktree is untrusted, or "
                "when a live session already holds the name. The session "
                "defaults to skip-permissions, sonnet[1m] and medium effort."
            ),
            "params": [
                {"name": "project", "type": "string", "required": True,
                 "description": "The project the scope belongs to."},
                {"name": "scope", "type": "string", "required": True,
                 "description": "The scope whose worktree is the session's directory."},
                {"name": "prompt", "type": "string",
                 "description": "The first task. Without one the session starts empty."},
                {"name": "name", "type": "string",
                 "description": "The session's name; defaults to the scope."},
                {"name": "model", "type": "string",
                 "description": "A model alias or full name."},
                {"name": "effort", "type": "string",
                 "description": "low, medium, high, xhigh or max."},
                {"name": "permission", "type": "string",
                 "description": "A permission mode (plan, acceptEdits, auto, "
                                "manual, dontAsk, bypassPermissions). Omitted "
                                "means skip-permissions."},
                {"name": "mode", "type": "string",
                 "description": "A label recorded with the session, which a "
                                "gateway gate can key on. Defaults to worker."},
                {"name": "disallowed_tools", "type": "array",
                 "description": "Tool names the session may not use."},
                {"name": "remote_control", "type": "boolean",
                 "description": "Enable Remote Control, named for the session."},
            ],
            "timeout": 1800,
            "effect": "write",
        },
        {
            "name": "list",
            "tool": "cx_list",
            "description": (
                "List the sessions on this node: background jobs with their "
                "state, parent, caller and mode, interactive terminal sessions "
                "with a `tmux attach` command, and the warm pool. Read this to "
                "see what is running; the pool summary says whether a Claude "
                "Code daemon runs at all, the binary version the pool seeds "
                "against and when the reconcile loop last ticked."
            ),
            "params": [
                {"name": "project", "type": "string",
                 "description": "Only sessions in this project."},
                {"name": "scope", "type": "string",
                 "description": "Only sessions in this scope."},
            ],
            "effect": "read",
        },
        {
            "name": "stop",
            "tool": "cx_stop",
            "description": (
                "Stop a session that `start` created. The conversation is "
                "kept and `claude attach <job>` reopens it. Refuses a job "
                "that `start` did not create."
            ),
            "params": [
                {"name": "job", "type": "string", "required": True,
                 "description": "The job id `start` returned."},
            ],
            "timeout": 30,
            "effect": "write",
        },
        {
            "name": "claim",
            "tool": "cx_claim",
            "description": (
                "Take the warm session, move it to `cwd`, and return its id for "
                "`claude attach`. An empty id means there was nothing warm, "
                "which the caller answers with an ordinary cold launch. This is "
                "what the `cx` command calls; a person has no reason to."
            ),
            "params": [
                {"name": "cwd", "type": "string", "required": True,
                 "description": "The directory to move the session to."},
            ],
            "timeout": 9,
            "effect": "write",
        },
        {
            "name": "seed",
            "tool": "cx_seed",
            "description": (
                "Start one warm session now and return its id. Refuses, with "
                "the reason, when no Claude Code daemon is already running: "
                "seeding then would put every background session on this node "
                "inside awm's control group, where the next deploy kills it. "
                "The reconcile loop calls this on its own; reach for it by "
                "hand only to see what a seed does or why one is refused."
            ),
            "params": [],
            "timeout": 60,
            "effect": "write",
        },
        {
            "name": "remove",
            "tool": "cx_remove",
            "description": (
                "List the sessions this service may delete, with the reason "
                "each one qualifies. Deletes nothing unless `apply` is true. "
                "A session that has been renamed, prompted, or claimed and is "
                "still alive never appears here: once somebody has taken a "
                "session it is theirs, and the pool waits for the daemon's own "
                "retirement instead."
            ),
            "params": [
                {"name": "apply", "type": "boolean",
                 "description": "Carry out the plan (default false)."},
            ],
            "timeout": 120,
            "effect": "write",
        },
    ],
    "emitters": [],
    "sessions": [],
}

def _on_start() -> None:
    """Start the reconcile loop, unless this process must not own the pool.

    Through `spawn_supervised` so a loop that dies on its first line is logged
    and respawned, rather than leaving the service registered and healthy with
    the warm session silently absent.
    """
    try:
        lifecycle.ensure_starts_dir()
    except lifecycle.Refused as exc:
        log.warning("cx: %s", exc)
    why = LOOP.enabled()
    if why:
        log.info("cx: not reconciling — %s", why)
        return
    spawn_supervised("cx.reconcile", LOOP.run)


async def _seed(args: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"session": await seed.seed_one()}
    except seed.Refused as exc:
        return {"session": None, "refused": str(exc)}


async def _remove(args: dict[str, Any]) -> dict[str, Any]:
    if args.get("apply"):
        return {"applied": await remove.apply()}
    return {"plan": remove.plan()}


async def _claim(args: dict[str, Any]) -> dict[str, Any]:
    out = await claim.claim(args.get("cwd") or "")
    if out.get("session"):
        # Seed the replacement while the caller is still starting up, rather
        # than up to a tick later.
        LOOP.kick()
    return out


async def _list(args: dict[str, Any], as_: str | None = None) -> dict[str, Any]:
    reason = lifecycle.caller_refusal(as_)
    if reason:
        return {"ok": False, "reason": reason}
    rows = await asyncio.to_thread(
        lifecycle.collect, args.get("project") or None, args.get("scope") or None)
    return {"sessions": rows, "pool": pool.status(LOOP)}


async def _start(args: dict[str, Any], as_: str | None = None) -> dict[str, Any]:
    return await lifecycle.start(args, as_)


async def _stop(args: dict[str, Any], as_: str | None = None) -> dict[str, Any]:
    return await lifecycle.stop(args, as_)


HANDLERS: dict[str, Any] = {
    "start": _start,
    "list": _list,
    "stop": _stop,
    "claim": _claim,
    "seed": _seed,
    "remove": _remove,
}


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter("cx", API_MANIFEST, HANDLERS,
                         on_start=_on_start).run()


if __name__ == "__main__":
    asyncio.run(main())
