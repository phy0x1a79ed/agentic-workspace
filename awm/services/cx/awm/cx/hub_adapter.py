"""Hub adapter for the `cx` service — Claude Code sessions on this node.

`start_other`, `list_other` and `stop_other` are the session lifecycle other
agents use; they sit in the `reflection` tool beside the verbs that act on the
calling session. The rest
of the service is the warm start: it keeps one background Claude Code session
idling so that `cx` in a terminal attaches to a renderer that has already
started, instead of paying a cold launch. The session is seeded in a neutral
directory and moved to the caller's directory at claim time, so the pool is not
keyed by directory and the first `cx` in a project is as fast as the hundredth.

Every function publishes a ``tool`` named ``reflection_<verb>_other``, matching
the service's ``tool = "reflection"``, while ``name`` stays the service-internal
handle behind ``/svc/cx/fn/<name>``. So the surface is ``awm reflection list_other`` and
``mcp__awm__reflection {verb:"list_other"}``. The pool verbs are hidden from
MCP.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.cx.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.config import privacy
from awm.gatewayclient import ServiceAdapter, spawn_supervised

from awm.cx import claim, lifecycle, pool, reconcile, remove, seed

log = logging.getLogger("awm.cx.hub_adapter")

LOOP = reconcile.Loop()

API_MANIFEST: dict[str, Any] = {
    "description": (
        "Claude Code sessions on this node. `start_other` creates a named "
        "background session in a scope's worktree, `list_other` shows every "
        "session with its lineage, and `stop_other` ends one that "
        "`start_other` created. The service also keeps one warm session idling "
        "so the `cx` command in a terminal attaches to an already-painted "
        "renderer; `claim_other`, `seed_other` and `remove_other` operate that "
        "pool, and `cx` itself is the everyday surface."
    ),
    "functions": [
        {
            "name": "start",
            "tool": "reflection_start_other",
            "description": (
                "Start a named background Claude Code session in a scope's "
                "worktree, creating the worktree if it is absent, and return "
                "its job id and the `claude attach` command. Refuses on a "
                "station, for a caller that is not a domestic peer, when no "
                "Claude Code daemon runs, when the worktree is untrusted, or "
                "when a live session already holds the name. The session "
                "defaults to skip-permissions, opus and high effort. "
                "Every caller gets the same session, whoever it is."
            ),
            "params": [
                {"name": "project", "type": "string", "required": True,
                 "description": "The project the scope belongs to."},
                {"name": "scope", "type": "string", "required": True,
                 "description": "The scope whose worktree is the session's directory."},
                {"name": "prompt", "type": "string",
                 "description": "The first task. Without one the session starts empty."},
                {"name": "system_prompt", "type": "string",
                 "description": "Text appended to the system prompt "
                                "(claude --append-system-prompt). Unlike the "
                                "prompt, a compaction never rewrites it."},
                {"name": "autocompact", "type": "integer",
                 "description": "The auto-compact window in tokens, 100000 to "
                                "1000000. Claude Code compacts about 33k below "
                                "it. Omitted means the model's own window."},
                {"name": "name", "type": "string",
                 "description": "The session's name; defaults to the scope."},
                {"name": "model", "type": "string",
                 "description": "A model alias or full name."},
                {"name": "effort", "type": "string",
                 "description": "low, medium, high, xhigh or max."},
                {"name": "permission", "type": "string",
                 "description": "A permission mode (plan, acceptEdits, auto, "
                                "manual, dontAsk, bypassPermissions). Omitted "
                                "means skip-permissions. The session starts in "
                                "this mode and can always switch to bypass, so "
                                "a plan-mode start runs in bypass once its plan "
                                "is approved."},
                {"name": "mode", "type": "string",
                 "description": "A label recorded with the session, which a "
                                "gateway gate can key on. Defaults to worker."},
                {"name": "disallowed_tools", "type": "array",
                 "description": "Tool names the session may not use."},
                {"name": "allowed_tools", "type": "array",
                 "description": "Tool names approved without a prompt, which "
                                "keeps a dontAsk session from having them "
                                "denied (for example mcp__awm__*)."},
                {"name": "tools", "type": "array",
                 "description": "The only built-in tools the session holds "
                                "(claude --tools). Omitted means all."},
                {"name": "restricted", "type": "boolean",
                 "description": "Confine file tools to the worktree and ignore "
                                "user settings (claude --restricted)."},
                {"name": "strict_mcp", "type": "boolean",
                 "description": "Load only the awm MCP server, from the "
                                "workspace config."},
                {"name": "remote_control", "type": "boolean",
                 "description": "Enable Remote Control, named for the session. "
                                "Off unless true. The result's `link` is the "
                                "claude.ai link, null with a `link_reason` if "
                                "it has not connected within a few seconds."},
                {"name": "pin", "type": "boolean",
                 "description": "Pin the job, so the daemon's idle retirement "
                                "passes it over. A pinned job is a kept "
                                "session: stopping it unpins it and removes it "
                                "with `claude rm`."},
                {"name": "brief", "type": "boolean",
                 "description": "Append the worker lifecycle brief to the system "
                                "prompt: work in the right scope, ask Tony "
                                "through the board, sign off and stop when done. "
                                "Default true. A pinned start never gets it."},
                {"name": "started_by", "type": "string",
                 "description": "Who the brief says started the session when no "
                                "session called, such as 'Tony from Discord'. "
                                "A calling session is named instead."},
            ],
            "timeout": 1800,
            "effect": "write",
        },
        {
            "name": "list",
            "tool": "reflection_list_other",
            "description": (
                "List the sessions on this node: background jobs with their "
                "state, parent, caller and mode, interactive terminal sessions "
                "with a `tmux attach` command, and the warm pool. Each row's "
                "`remote_control` is whether Remote Control is on now and `link` "
                "is its claude.ai link. Read this to "
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
            "tool": "reflection_stop_other",
            "description": (
                "Stop a session that `start_other` created. The conversation is "
                "kept and `claude attach <job>` reopens it, except for a pinned "
                "job, which is unpinned and removed. Refuses a job "
                "that `start_other` did not create."
            ),
            "params": [
                {"name": "job", "type": "string", "required": True,
                 "description": "The job id `start_other` returned."},
            ],
            "timeout": 30,
            "effect": "write",
        },
        {
            "name": "resume",
            "tool": "reflection_resume_other",
            "description": (
                "Bring back a stopped or retired job that `start_other` "
                "created, under the same job id and with its own conversation "
                "(`claude respawn`), and re-pin it if it was pinned. Refuses a "
                "running job, a removed job, a job whose transcript is gone, and "
                "a job that `start_other` did not create."
            ),
            "params": [
                {"name": "job", "type": "string", "required": True,
                 "description": "The job id `start_other` returned."},
            ],
            "timeout": 90,
            "effect": "write",
        },
        {
            "name": "claim",
            "tool": "reflection_claim_other",
            "surfaces": ["cli", "http"],
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
            "tool": "reflection_seed_other",
            "surfaces": ["cli", "http"],
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
            "tool": "reflection_remove_other",
            "surfaces": ["cli", "http"],
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
    held = pool.status(LOOP)
    if privacy.from_peer(as_):
        held["sessions"] = privacy.visible_rows(held["sessions"], as_)
        shown = {r["session"] for r in held["sessions"]}
        held["warm"] = [s for s in held["warm"] if s in shown]
    return {"sessions": privacy.visible_rows(rows, as_), "pool": held}


async def _start(args: dict[str, Any], as_: str | None = None) -> dict[str, Any]:
    return await lifecycle.start(args, as_)


async def _stop(args: dict[str, Any], as_: str | None = None) -> dict[str, Any]:
    return await lifecycle.stop(args, as_)


async def _resume(args: dict[str, Any], as_: str | None = None) -> dict[str, Any]:
    return await lifecycle.resume(args, as_)


HANDLERS: dict[str, Any] = {
    "start": _start,
    "list": _list,
    "stop": _stop,
    "resume": _resume,
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
