"""Hub adapter for the reflection service.

Boots the service as a gateway-registered process and runs the shared
:class:`awm.gatewayclient.ServiceAdapter` loop (register → ready → serve →
reconnect). The service owns no database, so there is no ``on_start``.

Surface: every verb projects onto MCP + CLI + HTTP — the MCP surface is the whole
point (an agent calls ``reflection(verb="compact")`` on itself, with no arguments).

- ``send``    — type any text/slash command into your own prompt and submit it.
- ``compact`` — sugar for ``send`` with ``text="/compact"``.
- ``mode``    — put your own session back into bypass-permissions mode.

Reflection acts on the caller and on nobody else, so nothing here takes a target.
This process does not share the caller's environment, but it does not need to:
the per-session ``awm-mcp`` proxy is a stdio child of the session calling it, so
the gateway stamps that proxy's parent pid onto the call as the caller's identity
before it arrives. ``awm.reflection.session_target`` turns that pid into either a
tmux pane or a background session's PTY socket, and the matching backend does the
typing. A caller the gateway cannot identify is refused rather than served with
somebody else's session.

Run via ``run.sh`` (which the hub spawns and respawns):
    python -m awm.reflection.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from awm.gatewayclient import ServiceAdapter

from awm.reflection import (
    daemon_inject,
    inject,
    pending,
    permission_mode,
    session_target,
    tmux_inject,
)

log = logging.getLogger("awm.reflection.hub_adapter")


API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "send",
            "tool": "reflection_send",
            "description": (
                "Type a command/text into YOUR OWN prompt and submit it (no Escape, "
                "so it QUEUES behind the current turn). Use for self-directed slash "
                "commands like /compact or /model opus. Works the same whether your "
                "session runs in a terminal or as a background job — you do not need "
                "to know which, and there is no way to aim this at another session. "
                "A submitted slash command is auto-trailed by a follow-up prompt "
                "(override with `followup`) so the session has a next turn once "
                "the command finishes — a bare command would otherwise go idle. "
                "The follow-up is DEFERRED (injected after the command completes, "
                "never queued behind it) so it can't run ahead of the command. "
                "Modal commands (/mcp, /status, /model with no arg, …) are refused "
                "— they trap input and freeze the session."
            ),
            "timeout": 60,
            "params": [
                {"name": "text", "type": "string", "required": True,
                 "description": "The line to inject, e.g. '/compact' or '/model opus'."},
                {"name": "enter", "type": "boolean",
                 "description": "Press Enter to submit after pasting (default true)."},
                {"name": "followup", "type": "string",
                 "description": "Prompt injected after a slash command completes to "
                                "keep the session alive (default 'Continue with what "
                                "you were doing.'). Ignored for plain prompts."},
                {"name": "delay_ms", "type": "integer",
                 "description": "Wait this many ms before injecting (default 0)."},
                {"name": "confirm", "type": "boolean",
                 "description": "Required to send destructive commands (/clear, /quit, /exit)."},
            ],
        },
        {
            "name": "compact",
            "tool": "reflection_compact",
            "description": (
                "Compact your own conversation: injects /compact into your own "
                "prompt; it queues and runs the instant the current turn ends. A "
                "resume prompt is then injected AFTER compaction completes (a "
                "detached watcher waits for it — the resume is never queued behind "
                "/compact, so it always lands on the freshly-compacted context) so "
                "the session resumes instead of going idle. Returns immediately "
                "with followup_deferred=true. Call with no arguments; works the "
                "same in a terminal session and in a background one."
            ),
            "timeout": 60,
            "params": [
                {"name": "followup", "type": "string",
                 "description": "Prompt queued after /compact to resume work "
                                "(default 'Continue with what you were doing.')."},
                {"name": "delay_ms", "type": "integer",
                 "description": "Wait this many ms before injecting (default 0)."},
            ],
        },
        {
            "name": "mode",
            "tool": "reflection_mode",
            "description": (
                "Put your own session back into bypass-permissions mode. Approving "
                "a plan drops the session into whatever mode it was in before "
                "planning — from a phone that is 'auto', where a classifier gates "
                "every action. This cycles the permission mode (the Shift+Tab "
                "cycle) until the session reads as bypass permissions, checking "
                "after each step rather than assuming a starting point. Already in "
                "bypass is a no-op. Acts only on your own session."
            ),
            "timeout": 60,
            "params": [],
        },
        {
            "name": "pending",
            "tool": "reflection_pending",
            "description": (
                "List the deferred resumes this service still owes — one per "
                "session that has a slash command in flight. A session that "
                "compacted and then went idle forever is the visible symptom of "
                "one of these being lost; this is how you tell whether the "
                "promise is still being watched or vanished with a restart. "
                "Reports on the whole node, not just the caller."
            ),
            "timeout": 30,
            "params": [],
        },
        {
            "name": "whoami",
            "tool": "reflection_whoami",
            "description": (
                "Report which session reflection resolves you to, and how it would "
                "reach you (a tmux pane, or a background session's PTY). Useful "
                "when a reflection call refuses and you want to see why."
            ),
            "timeout": 30,
            "params": [],
        },
    ],
    "emitters": [],
    "sessions": [],
}


def _bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    return v in (True, "true", "True", "1", 1)


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _caller_pid(args: dict) -> Any:
    """The calling session's pid, stamped by the gateway from an observed header.

    Never a model-supplied value: the gateway overwrites this key on every
    reflection call and strips it when it has no header to stamp.
    """
    return args.get("_caller_pid")


# Every failure mode that means "we could not do this to you" surfaces the same
# way — a result with ok=false and a readable reason, not an exception. The
# distinction the caller cares about is whether their command ran, not which
# layer declined.
_FAILURES = (session_target.ResolveError, tmux_inject.TmuxError,
             daemon_inject.DaemonError, inject.DeliveryError)


def _guarded(verb: str, fn):
    """Wrap a handler so no failure leaves this service without saying so.

    Every way a reflection call can fail used to end here as a plain result dict
    and nothing else: no log line, no on-disk trace. So "reflection didn't
    compact my session" was answerable only by reconstructing the moment from
    ``/proc``, the daemon roster and Claude Code's per-session records — long
    after the state that would have explained it had moved on.

    Both kinds of failure funnel through this one seam, at levels that say which
    is which. A transport or identity failure is a defect and logs at WARNING. A
    guard refusal — a destructive command without ``confirm``, a modal one that
    would freeze the session — is the service working as designed and logs at
    INFO, but it still has to appear, because from the caller's side the two look
    identical. Handling both here rather than inside each backend also means the
    tmux and daemon paths cannot drift on it.
    """
    def run(args: dict) -> dict:
        try:
            result = fn(args)
        except _FAILURES as exc:
            log.warning("reflection: %s failed for caller pid %s: %s",
                        verb, _caller_pid(args), exc)
            return {"ok": False, "error": str(exc)}
        if not result.get("ok"):
            # Never log the rest of `args`: for `send`, `text` is the caller's
            # own prompt content.
            log.info("reflection: %s refused for caller pid %s: %s", verb,
                     _caller_pid(args),
                     result.get("reason") or result.get("error"))
        return result
    return run


def _handle_send(args: dict) -> dict:
    return inject.send(
        args["text"],
        caller_pid=_caller_pid(args),
        enter=_bool(args.get("enter"), True),
        delay_ms=_int(args.get("delay_ms"), 0),
        confirm=_bool(args.get("confirm"), False),
        followup=args.get("followup"),
    )


def _handle_compact(args: dict) -> dict:
    return inject.send(
        "/compact",
        caller_pid=_caller_pid(args),
        delay_ms=_int(args.get("delay_ms"), 0),
        followup=args.get("followup"),
    )


def _handle_mode(args: dict) -> dict:
    return permission_mode.ensure_bypass(caller_pid=_caller_pid(args))


def _handle_whoami(args: dict) -> dict:
    return {"ok": True, **inject.describe_caller(_caller_pid(args))}


def _handle_pending(args: dict) -> dict:
    now = int(time.time() * 1000)
    return {"ok": True, "pending": [
        {"session": p.name or p.session_id, "pid": p.repl_pid,
         "hosting": p.hosting, "command": p.text, "resume": p.followup,
         "waiting_for_s": max(0, (now - p.injected_at_ms) // 1000),
         # A resume is held rather than typed into a session that would only
         # queue it, so a promise sitting here is normal and these two say
         # whether it is progressing or stuck.
         "holds": p.holds, "last_outcome": p.last_outcome}
        for p in pending.load_all()
    ]}


HANDLERS = {verb: _guarded(verb, fn) for verb, fn in {
    "send": _handle_send,
    "compact": _handle_compact,
    "mode": _handle_mode,
    "pending": _handle_pending,
    "whoami": _handle_whoami,
}.items()}


def _on_start() -> None:
    """Re-arm follow-ups promised by the process this one replaced.

    A deferred resume is a thread, and the gateway restarts this service often
    enough — deploys, crash-respawns, an operator `awm services restart` — that
    "the thread survives" was never a safe assumption. Cheap and non-blocking:
    a directory scan plus a thread per promise, and it must stay that way (see
    the ready-ASAP contract — a slow `on_start` reads as a broken service).

    An **overlay** must not do this. A shadow does not replace the base process,
    it only takes the prefix — so the base is still running, still holding its
    own watcher for every promise in that directory, and replaying here arms a
    second one. Both then fire, and the session gets the resume twice. Observed
    while shadowing this service to test it: the overlay's boot re-armed a
    promise a live base was already waiting on. Injecting a prompt a session did
    not ask for is the one thing this service must never do, so the base keeps
    sole ownership of promises it made.
    """
    if os.environ.get("AWM_SERVICE_OVERLAY"):
        log.info("reflection: running as an overlay; leaving pending resumes to "
                 "the base process that promised them")
        return
    try:
        outcome = inject.replay_pending()
    except Exception as exc:  # never let a bad record keep the service down
        log.warning("reflection: could not replay pending resumes: %s", exc)
        return
    if outcome["pending"]:
        log.info("reflection: %s pending resume(s) found on boot, %s re-armed",
                 outcome["pending"], outcome["resumed"])


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter("reflection", API_MANIFEST, HANDLERS,
                         on_start=_on_start).run()


if __name__ == "__main__":
    asyncio.run(main())
