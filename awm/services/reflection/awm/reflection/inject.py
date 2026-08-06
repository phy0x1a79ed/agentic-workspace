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

from awm.reflection import daemon_inject, session_target, tmux_inject

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
                              followup=followup)
    if result.get("ok"):
        result.setdefault("session", target.name or target.session_id)
        result.setdefault("hosting", "tmux")
    return result


def describe_caller(caller_pid: Optional[int]) -> dict[str, Any]:
    """What reflection thinks the caller is — useful when a refusal is confusing."""
    target = _resolve(caller_pid)
    common = {"session": target.name or target.session_id,
              "session_id": target.session_id, "pid": target.repl_pid}
    if isinstance(target, session_target.DaemonTarget):
        return {**common, "hosting": "background"}
    return {**common, "hosting": "tmux", "pane": target.pane}
