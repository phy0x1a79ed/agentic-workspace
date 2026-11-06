"""Hub adapter for the dsh service — the DeepSeek Harness.

Registers with the gateway on the shared ``ServiceAdapter`` loop (register →
ready → serve → reconnect), so the harness is a service like any other: visible
in ``awm services list``, health as a verb, and reachable from any node in the
fleet rather than being a node process somebody started by hand in a terminal.

Two things live under this one supervised process:

  - the harness itself, a node web server on loopback — see ``harness``,
  - one TLS front on the mesh behind awm's edge session — see ``front``.

Both die with this process. That is the cheaper trade here than the adoption
``claude-science`` needs: the harness persists its sessions to ``$DSH_HOME``, so
an awm deploy costs a browser reconnect rather than a conversation.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.dsh.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter, spawn_supervised

from awm.dsh import front, harness, sessions, settings

log = logging.getLogger("awm.dsh.hub_adapter")

SUPERVISOR = harness.Supervisor()

#: Every function carries an explicit ``tool`` name under a ``dsh_`` prefix, the
#: tool its awm-plugin.toml names. The tool is off the MCP surface; the verbs answer
#: on the CLI (``awm dsh status``) and over HTTP.
API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "status",
            "tool": "dsh_status",
            "description": (
                "Report the DeepSeek Harness: whether the fork worktree is "
                "built and at what version, which revision it is on and "
                "whether that revision is what was built, whether the web "
                "server is running and at what pid/port, whether it is "
                "actually listening, the mesh front's listener and TLS state "
                "with the URL to open, and the model route's two halves — "
                "provider declared, key available — named separately."
            ),
            "params": [],
        },
        {
            "name": "start",
            "tool": "dsh_start",
            "description": (
                "Start the harness web server on loopback, waiting until it "
                "binds. Idempotent."
            ),
            "params": [],
            "timeout": 120,
        },
        {
            "name": "stop",
            "tool": "dsh_stop",
            "description": (
                "Stop the harness web server, ending live GUI connections. "
                "Sessions are persisted, so this loses no conversation. "
                "Idempotent. The supervision loop will not respawn it until "
                "start is called again."
            ),
            "params": [],
            "timeout": 60,
        },
        {
            "name": "restart",
            "tool": "dsh_restart",
            "description": (
                "Stop the harness and start it again — e.g. to pick up an edited "
                "settings.yaml, which is read at startup."
            ),
            "params": [],
            "timeout": 180,
        },
        {
            "name": "url",
            "tool": "dsh_url",
            "description": (
                "The mesh URL that opens the harness GUI. Behind awm's edge "
                "session, so it is usable only by someone who could log into awm."
            ),
            "params": [],
        },
        {
            "name": "model",
            "tool": "dsh_model",
            "description": (
                "The default model new sessions start on. With no arguments, "
                "report the current selection and the route's catalog. Given a "
                "model id, select it — the harness watches its settings "
                "document, so this takes effect without a restart. This exists "
                "because the harness's own Settings UI is loopback-only: a "
                "browser on the mesh gets an in-memory settings mirror and the "
                "Models page reports that settings are unavailable."
            ),
            "params": [
                {"name": "model", "type": "string",
                 "description": "Model id to select. Omit to report only."},
                {"name": "declare", "type": "boolean",
                 "description": "Also add the id to the route's catalog. Needed "
                                "for a model the route does not yet advertise."},
                {"name": "reasoning", "type": "string",
                 "description": "Reasoning effort to set alongside the model. "
                                "Omit to keep whatever is already selected."},
            ],
            "timeout": 60,
        },
        {
            "name": "logs",
            "tool": "dsh_logs",
            "description": "Tail the harness's stdout/stderr log.",
            "params": [
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
# Every handler hops to a worker thread: they spawn processes, signal groups and
# poll sockets, and any of those on the event loop would stall the control WS
# and have the gateway take this service for dead.


async def _h_status(args: dict) -> dict:
    return {
        "harness": await asyncio.to_thread(SUPERVISOR.snapshot),
        "front": front.status(),
    }


async def _h_start(args: dict) -> dict:
    return await asyncio.to_thread(SUPERVISOR.start)


async def _h_stop(args: dict) -> dict:
    return await asyncio.to_thread(SUPERVISOR.stop)


async def _h_restart(args: dict) -> dict:
    return await asyncio.to_thread(SUPERVISOR.restart)


async def _h_url(args: dict) -> dict:
    return {"url": front.origin(), "note": "requires an awm session"}


def _model(args: dict) -> dict:
    """Report or set the default model. Runs on a worker thread (file I/O + lock)."""
    model = (args.get("model") or "").strip()
    if not model:
        doc = settings.read_document()
        return {"selection": settings.selection(doc),
                "models": settings.declared_models(doc),
                "settings": str(harness.SETTINGS_FILE)}
    return settings.set_default_model(
        model, declare=bool(args.get("declare")),
        reasoning=(args.get("reasoning") or "").strip() or None)


async def _h_model(args: dict) -> dict:
    return await asyncio.to_thread(_model, args)


async def _h_logs(args: dict) -> dict:
    tail = int(args.get("tail") or 200)
    return {"tail": tail, "log": await asyncio.to_thread(SUPERVISOR.logs, tail)}


# -- harness-ds: the harness contract (harness-contract.md § dsh) -----------


def _fork_pid() -> int | None:
    return SUPERVISOR.snapshot().get("pid")


def _contract(fn):
    """A contract verb: run off the event loop, and a fork fault reads as ok=false."""
    async def run(args: dict) -> dict:
        try:
            return await asyncio.to_thread(fn, args)
        except sessions.ForkError as exc:
            return {"ok": False, "error": str(exc)}
    return run


def _hs_find(args: dict) -> dict:
    row = sessions.find_one(args.get("ref") or "")
    return {"ok": True, "matches": [row] if row else []}


def _hs_status(args: dict) -> dict:
    try:
        row = sessions.target(args, _fork_pid())
    except sessions.ForkError as exc:
        if not harness.listening():
            return {"ok": True, "live": False, "state": "stopped"}
        return {"ok": False, "error": str(exc), "unknown_ref": args.get("session_id")
                or args.get("ref")}
    return {"ok": True, **row}


def _hs_list(args: dict) -> dict:
    rows = sessions.rows()
    for key in ("project", "scope"):
        if args.get(key):
            rows = [r for r in rows if r.get(key) == args[key]]
    return {"ok": True, "sessions": rows}


def _hs_send(args: dict) -> dict:
    if args.get("followup"):
        return sessions._unsupported("type a follow-up after any command but /compact")
    return sessions.send(sessions.target(args, _fork_pid()), args["text"])


_CONTRACT_HANDLERS = {
    "platform": lambda a: sessions.platform(),
    "resolve": lambda a: sessions.resolve(a.get("pid"), _fork_pid()),
    "find": _hs_find,
    "status": _hs_status,
    "list": _hs_list,
    "send": _hs_send,
    "interrupt": lambda a: sessions.interrupt(sessions.target(a, _fork_pid())),
    "start": lambda a: sessions.start(a, workspace=sessions.workspace_root(),
                                      provider=sessions.provider_name()),
    "stop": lambda a: sessions.stop(sessions.target(a, _fork_pid())),
    "resume": lambda a: sessions.resume(sessions.target(a, _fork_pid()), a.get("prompt")),
    "set": lambda a: sessions.set_(sessions.target(a, _fork_pid()), a,
                                   sessions.provider_name()),
    "compact": lambda a: sessions.compact(sessions.target(a, _fork_pid()),
                                          a.get("followup")),
}

_TARGET = [{"name": n, "type": t, "required": False} for n, t in
           (("session_id", "string"), ("ref", "string"), ("pid", "integer"))]
_CONTRACT_PARAMS = {
    "platform": [], "resolve": [{"name": "pid", "type": "integer", "required": True}],
    "find": [{"name": "ref", "type": "string", "required": True}],
    "status": _TARGET, "interrupt": _TARGET, "stop": _TARGET + [
        {"name": "keep", "type": "boolean"}, {"name": "confirm", "type": "boolean"}],
    "list": [{"name": "project", "type": "string"}, {"name": "scope", "type": "string"},
             {"name": "include_stopped", "type": "boolean"}],
    "send": _TARGET + [{"name": n, "type": t} for n, t in (
        ("text", "string"), ("submit", "boolean"), ("confirm", "boolean"),
        ("followup", "string"), ("delay_ms", "integer"))],
    "start": [{"name": n, "type": t} for n, t in (
        ("project", "string"), ("scope", "string"), ("prompt", "string"),
        ("name", "string"), ("model", "string"), ("effort", "string"),
        ("preset", "string"), ("trust", "string"), ("task_id", "string"),
        ("role", "string"), ("started_by", "string"), ("pin", "boolean"),
        ("suggest", "boolean"), ("isolated", "boolean"), ("pid", "integer"))],
    "resume": _TARGET + [{"name": "prompt", "type": "string"}],
    "set": _TARGET + [{"name": n, "type": "string"} for n in ("model", "effort", "name", "cwd")],
    "compact": _TARGET + [{"name": "followup", "type": "string"},
                          {"name": "delay_ms", "type": "integer"}],
}

for _verb in _CONTRACT_HANDLERS:
    API_MANIFEST["functions"].append({
        "name": f"harness_{_verb}", "tool": f"harness-ds_{_verb}",
        "surfaces": ["cli", "http"], "timeout": 60,
        "description": f"Harness contract verb `{_verb}` for DeepSeek harness sessions.",
        "params": _CONTRACT_PARAMS[_verb]})


HANDLERS = {
    **{f"harness_{v}": _contract(fn) for v, fn in _CONTRACT_HANDLERS.items()},
    "status": _h_status,
    "start": _h_start,
    "stop": _h_stop,
    "restart": _h_restart,
    "url": _h_url,
    "model": _h_model,
    "logs": _h_logs,
}


# -- the supervision loop ---------------------------------------------------


async def _health_loop() -> None:
    """Respawn the harness whenever it has actually died. Never exits.

    Watches process liveness rather than an HTTP probe: a slow probe while the
    harness is streaming a long completion is not evidence of death, and
    respawning on it would cut the response it was mistaking for a hang.
    """
    log.info("dsh: supervision loop started (interval=%ss)",
             harness.HEALTH_INTERVAL_S)
    while True:
        try:
            await asyncio.sleep(harness.HEALTH_INTERVAL_S)
            res = await asyncio.to_thread(SUPERVISOR.reconcile)
            if res.get("action") == "respawned":
                log.warning("dsh: harness respawned (previous exit %s)",
                            res.get("previous_exit"))
        except Exception:  # noqa: BLE001 — never let the loop die
            # CancelledError is a BaseException and so passes through, which is
            # what the supervisor above wants: a *return* from here would read as
            # a defect and be respawned, but a cancellation is a shutdown.
            log.exception("dsh: supervision pass failed")


async def _on_start() -> None:
    """Start the harness, raise the front, then loop.

    No failure here is fatal. The service still registers, so ``status`` can
    report *why* it is broken, and the loop keeps retrying the harness.
    """
    # Before the first spawn: the harness reads settings.yaml at startup, so a
    # route seeded afterwards would not take effect until the next restart.
    try:
        seeded = await asyncio.to_thread(settings.ensure)
        log.info("dsh: model route %s", "seeded" if seeded.get("changed")
                 else "already declared")
    except Exception:  # noqa: BLE001 — an unroutable harness still registers
        log.exception("dsh: could not seed the model route")

    if not harness.installed():
        log.warning("dsh: no built harness at %s — run install.sh; the service "
                    "will register and report this via status", harness.CLI)
    else:
        try:
            snap = await asyncio.to_thread(SUPERVISOR.start)
            log.info("dsh: harness pid=%s version=%s port=%s listening=%s",
                     snap.get("pid"), snap.get("version"), snap.get("port"),
                     snap.get("listening"))
        except Exception:  # noqa: BLE001
            log.exception("dsh: initial start failed; the loop will retry")

    try:
        front.start()
    except Exception:  # noqa: BLE001 — the harness is still usable on loopback
        log.exception("dsh: mesh front failed to start")

    # A dead supervision loop looks exactly like a harness that has not crashed,
    # so it is spawned supervised rather than as a bare task nobody reads.
    spawn_supervised("dsh:health", _health_loop)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter("dsh", API_MANIFEST, HANDLERS, on_start=_on_start).run()


if __name__ == "__main__":
    asyncio.run(main())
