"""The service's front door: resolve who is calling, then inject into them.

Callers reach reflection with nothing but what they want typed. Everything about
*where* that goes is derived here from the caller's own process identity, which
the gateway observed rather than accepted as an argument. Two backends sit behind
this — tmux for terminal sessions, the Claude Code daemon's PTY socket for
background ones — and which one runs is a detail of how the caller happens to be
hosted, not something they choose or can see.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from awm.reflection import daemon_inject, guards, pending, session_target, tmux_inject

log = logging.getLogger("awm.reflection.inject")


def _resolve(caller_pid: Optional[int]):
    if not caller_pid:
        raise session_target.ResolveError(
            "could not tell which session is calling, so there is nothing safe "
            "to inject into. Reflection types into the caller's own prompt and "
            "will not guess at a target — this usually means the call did not "
            "come through a session's own awm-mcp proxy (a plain shell, for "
            "instance)")
    return session_target.resolve(caller_pid)


def send(text: str, *, caller_pid: Optional[int], enter: bool = True,
         delay_ms: int = 0, confirm: bool = False,
         followup: Optional[str] = None) -> dict:
    """Inject ``text`` into the calling session, whatever is hosting it."""
    target = _resolve(caller_pid)
    # Ask the guards before announcing anything. The backends are still the
    # authority and each enforces this itself — but they do so *after* this
    # function has already logged "injecting …", which made the log claim a
    # refused `/clear` had gone in. A log that overstates what happened is worse
    # than no log on the one path where someone is trying to find out why a
    # command did not run.
    refused = guards.refusal(text, confirm=confirm)
    if refused is not None:
        return refused
    if isinstance(target, session_target.DaemonTarget):
        log.info("reflection: injecting %r into background session %s",
                 text, target.name or target.session_id)
        return daemon_inject.send(text, target=target, enter=enter,
                                  delay_ms=delay_ms, confirm=confirm,
                                  followup=followup)
    log.info("reflection: injecting %r into pane %s (session %s)",
             text, target.pane, target.name or target.session_id)
    result = tmux_inject.send(text, pane=target.pane, enter=enter,
                              delay_ms=delay_ms, confirm=confirm,
                              followup=followup, repl_pid=target.repl_pid,
                              session_id=target.session_id, name=target.name)
    if result.get("ok"):
        result.setdefault("session", target.name or target.session_id)
        result.setdefault("hosting", "tmux")
    return result


def replay_pending() -> dict[str, Any]:
    """Re-arm the watchers for follow-ups this service promised and did not deliver.

    Called once on boot. The gateway drains and respawns its services routinely —
    a restart, a deploy, a crash-respawn — and each of those takes every in-flight
    watcher thread with it. Without this pass a session that compacted at the
    wrong moment is simply left idle forever, with the caller already told the
    resume was on its way and nothing anywhere logging that it never came.

    Re-resolving from the pid rather than from the stored address is deliberate:
    a background session that respawned has a new PTY socket and token, and an
    interactive one may have moved panes. What is checked against the stored
    record is the caller's ``procStart`` — a pid outlives nothing, and a promise
    replayed against whatever inherited the number would type into a stranger.
    """
    items = pending.load_all()
    resumed = 0
    for item in items:
        live = session_target._proc_start(item.repl_pid)
        if live is None:
            log.info("reflection: session %s (pid %s) is gone; dropping its "
                     "pending resume", item.name or item.session_id, item.repl_pid)
            pending.clear(item.repl_pid)
            continue
        if item.proc_start and item.proc_start != live:
            log.warning("reflection: pid %s was recycled since its resume was "
                        "promised (started %s, now %s); dropping it rather than "
                        "injecting into whatever holds that pid now",
                        item.repl_pid, item.proc_start, live)
            pending.clear(item.repl_pid)
            continue
        try:
            target = session_target.resolve(item.repl_pid)
        except session_target.ResolveError as exc:
            log.warning("reflection: cannot reach session %s to deliver its "
                        "pending resume: %s", item.name or item.session_id, exc)
            pending.clear(item.repl_pid)
            continue
        log.info("reflection: re-arming the deferred resume for session %s "
                 "(%r was injected before this service restarted)",
                 item.name or item.session_id, item.text)
        if isinstance(target, session_target.DaemonTarget):
            daemon_inject.resume_watch(item, target)
        else:
            tmux_inject.resume_watch(item, target.pane)
        resumed += 1
    return {"pending": len(items), "resumed": resumed}


def describe_caller(caller_pid: Optional[int]) -> dict[str, Any]:
    """What reflection thinks the caller is — useful when a refusal is confusing."""
    target = _resolve(caller_pid)
    common = {"session": target.name or target.session_id,
              "session_id": target.session_id, "pid": target.repl_pid}
    if isinstance(target, session_target.DaemonTarget):
        return {**common, "hosting": "background"}
    return {**common, "hosting": "tmux", "pane": target.pane}
