"""Hub adapter for the reflection service.

Boots the service as a gateway-registered process and runs the shared
:class:`awm.gatewayclient.ServiceAdapter` loop (register → ready → serve →
reconnect). The service owns no database, so there is no ``on_start``.

Surface: every verb projects onto MCP + CLI + HTTP — the MCP surface is the whole
point (an agent calls ``reflection(verb="compact")`` on itself, with no arguments).

- ``message_self`` — type any text/slash command into your own prompt and submit it.
- ``compact`` — sugar for ``message_self`` with ``text="/compact"``.
- ``mode``    — put your own session back into bypass-permissions mode.

Those three act on the caller and on nobody else. The session-control verbs
(``remote_control``, ``model``, ``effort``, ``rename``, ``interrupt``, ``stop``,
``link``, ``message``; see :mod:`awm.reflection.control`) take an optional ``job``
that names any other session on this node, and act on the caller's own without
one. They never reach another node.

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

from awm import claudedaemon
from awm.reflection import (
    control,
    inject,
    oc_inject,
    oc_session,
    pending,
    permission_mode,
    session_target,
    tmux_inject,
)

log = logging.getLogger("awm.reflection.hub_adapter")


API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "message_self",
            "effect": "write",
            "tool": "reflection_message_self",
            "description": (
                "Type a command/text into YOUR OWN prompt and submit it (no Escape, "
                "so it QUEUES behind the current turn). Use for self-directed slash "
                "commands like /compact or /model opus. Works the same whether your "
                "session runs in a terminal or as a background job — you do not need "
                "to know which. It types only into your own session; `message` types "
                "into another. "
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
            "effect": "write",
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
            "effect": "write",
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
            "params": [
                {"name": "expect_session", "type": "string",
                 "description": "Refuse unless the resolved session has this "
                                "sessionId. For hooks, which reach us through an "
                                "ancestry walk; agents never need it."},
            ],
        },
        {
            "name": "remote_control",
            "effect": "write",
            "tool": "reflection_remote_control",
            "description": (
                "Turn Remote Control on or off for a session on this node. On "
                "types `/rc <name>` and returns the claude.ai link once it "
                "connects, or link=null with a link_reason. Off types `/rc` and "
                "selects 'Disconnect this session' in the modal it opens; it is "
                "refused if the session is mid-turn or the modal is not the "
                "expected one. Your own session cannot answer in the same turn: "
                "the command runs when the turn ends and a follow-up prompt "
                "resumes you."
            ),
            "timeout": 60,
            "params": [
                {"name": "on", "type": "boolean", "required": True,
                 "description": "true to turn Remote Control on, false to turn it off."},
                {"name": "name", "type": "string",
                 "description": "The Remote Control name when turning on; a plain label."},
                {"name": "job", "type": "string",
                 "description": "A job id, or a session name as `list_other` shows "
                                "it. Omit to act on your own session."},
            ],
        },
        {
            "name": "model",
            "effect": "write",
            "tool": "reflection_model",
            "description": (
                "Switch a session's model: types `/model <model>` and submits "
                "it. Queues behind a running turn. Refused while a modal or "
                "permission prompt is showing."
            ),
            "timeout": 60,
            "params": [
                {"name": "model", "type": "string", "required": True,
                 "description": "A model alias or full name, e.g. opus or sonnet[1m]."},
                {"name": "job", "type": "string",
                 "description": "A job id, or a session name as `list_other` shows "
                                "it. Omit to act on your own session."},
            ],
        },
        {
            "name": "effort",
            "effect": "write",
            "tool": "reflection_effort",
            "description": (
                "Set a session's effort: types `/effort <level>` and submits "
                "it. Queues behind a running turn. Refused while a modal or "
                "permission prompt is showing."
            ),
            "timeout": 60,
            "params": [
                {"name": "level", "type": "string", "required": True,
                 "description": "low, medium, high, xhigh or max."},
                {"name": "job", "type": "string",
                 "description": "A job id, or a session name as `list_other` shows "
                                "it. Omit to act on your own session."},
            ],
        },
        {
            "name": "rename",
            "effect": "write",
            "tool": "reflection_rename",
            "description": (
                "Rename a session: types `/rename <name>` and submits it. "
                "Queues behind a running turn. Refused while a modal or "
                "permission prompt is showing."
            ),
            "timeout": 60,
            "params": [
                {"name": "name", "type": "string", "required": True,
                 "description": "The new name, one short line."},
                {"name": "job", "type": "string",
                 "description": "A job id, or a session name as `list_other` shows "
                                "it. Omit to act on your own session."},
            ],
        },
        {
            "name": "interrupt",
            "effect": "write",
            "tool": "reflection_interrupt",
            "description": (
                "Interrupt a session's running turn: presses Escape once, as a "
                "person would. Refused while a modal or permission prompt is "
                "showing."
            ),
            "timeout": 60,
            "params": [{"name": "job", "type": "string",
                 "description": "A job id, or a session name as `list_other` shows "
                                "it. Omit to act on your own session."}],
        },
        {
            "name": "stop",
            "effect": "write",
            "tool": "reflection_stop",
            "description": (
                "Stop a background session on this node and remove its job "
                "(`claude stop`, then `claude rm`). Works on any session, not "
                "only ones `start_other` created. Your own session is refused "
                "unless confirm is true, and is then stopped after this call "
                "returns. A terminal session is refused."
            ),
            "timeout": 60,
            "params": [
                {"name": "job", "type": "string", "required": True,
                 "description": "A job id, or a session name as `list_other` shows it."},
                {"name": "confirm", "type": "boolean",
                 "description": "Required to stop your own session."},
            ],
        },
        {
            "name": "link",
            "effect": "read",
            "tool": "reflection_link",
            "description": (
                "Read a session's claude.ai link and whether Remote Control is "
                "on, from its record. Types nothing."
            ),
            "timeout": 30,
            "params": [{"name": "job", "type": "string",
                 "description": "A job id, or a session name as `list_other` shows "
                                "it. Omit to act on your own session."}],
        },
        {
            "name": "message",
            "effect": "write",
            "tool": "reflection_message",
            "description": (
                "Type text into another session's prompt and submit it (no "
                "Escape, so it queues behind its current turn). Same guards as "
                "`message_self`: destructive commands need confirm, modal "
                "commands are refused, and so is a session showing a modal or "
                "permission prompt. No follow-up is added."
            ),
            "timeout": 60,
            "params": [
                {"name": "job", "type": "string", "required": True,
                 "description": "A job id, or a session name as `list_other` shows it."},
                {"name": "text", "type": "string", "required": True,
                 "description": "The line to type."},
                {"name": "confirm", "type": "boolean",
                 "description": "Required to send destructive commands (/clear, /quit, /exit)."},
            ],
        },
        {
            "name": "pending",
            "effect": "read",
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
            "effect": "read",
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
_FAILURES = (session_target.ResolveError, oc_session.ResolveError,
             tmux_inject.TmuxError, claudedaemon.DaemonError,
             oc_inject.OpencodeError, oc_inject.ServeError,
             inject.DeliveryError)


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
            # Never log the rest of `args`: for `message_self`, `text` is the caller's
            # own prompt content.
            log.info("reflection: %s refused for caller pid %s: %s", verb,
                     _caller_pid(args),
                     result.get("reason") or result.get("error"))
        return result
    return run


def _handle_message_self(args: dict) -> dict:
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
    return permission_mode.ensure_bypass(
        caller_pid=_caller_pid(args),
        expect_session=args.get("expect_session") or None)


def _handle_whoami(args: dict) -> dict:
    return {"ok": True, **inject.describe_caller(_caller_pid(args))}


def _handle_pending(args: dict) -> dict:
    now = int(time.time() * 1000)
    return {"ok": True, "pending": [
        {"session": p.name or p.session_id, "pid": p.repl_pid,
         "hosting": p.hosting, "command": p.text, "resume": p.followup,
         "waiting_for_s": max(0, (now - p.injected_at_ms) // 1000),
         # A resume is held rather than typed into a session that would only
         # queue it, so a promise sitting here is normal and these say whether it
         # is progressing, and how many times the session has been asked to bring
         # its turn to a close so the command it is sitting on can run.
         "holds": p.holds, "nudges": p.nudges,
         "last_outcome": p.last_outcome}
        for p in pending.load_all()
    ]}


def _job(args: dict) -> Any:
    return args.get("job") or None


def _handle_remote_control(args: dict) -> dict:
    return control.remote_control(
        _bool(args.get("on")), name=args.get("name") or None, job=_job(args),
        caller_pid=_caller_pid(args))


def _handle_model(args: dict) -> dict:
    return control.model(args.get("model"), job=_job(args),
                         caller_pid=_caller_pid(args))


def _handle_effort(args: dict) -> dict:
    return control.effort(args.get("level"), job=_job(args),
                          caller_pid=_caller_pid(args))


def _handle_rename(args: dict) -> dict:
    return control.rename(args.get("name"), job=_job(args),
                          caller_pid=_caller_pid(args))


def _handle_interrupt(args: dict) -> dict:
    return control.interrupt(job=_job(args), caller_pid=_caller_pid(args))


def _handle_stop(args: dict) -> dict:
    return control.stop(_job(args), caller_pid=_caller_pid(args),
                        confirm=_bool(args.get("confirm")))


def _handle_link(args: dict) -> dict:
    return control.link(job=_job(args), caller_pid=_caller_pid(args))


def _handle_message(args: dict) -> dict:
    return control.message(_job(args), args.get("text"),
                           caller_pid=_caller_pid(args),
                           confirm=_bool(args.get("confirm")))


HANDLERS = {verb: _guarded(verb, fn) for verb, fn in {
    "message_self": _handle_message_self,
    "compact": _handle_compact,
    "mode": _handle_mode,
    "remote_control": _handle_remote_control,
    "model": _handle_model,
    "effort": _handle_effort,
    "rename": _handle_rename,
    "interrupt": _handle_interrupt,
    "stop": _handle_stop,
    "link": _handle_link,
    "message": _handle_message,
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
