"""Which session is calling this proxy — the identity reflection acts on.

Shared by both stdio proxies (:mod:`awm.gateway.mcp_stdio` and
:mod:`awm.gateway.mcp_server_sdk`), which stamp the result as the
``X-Awm-Session-Pid`` header. It lives apart from either so the two cannot drift:
a session that reflects on itself must resolve identically whichever proxy its
environment picked.

Stdlib only, deliberately — the fast proxy exists because framework import time
is dead time on every session launch, and this sits on that path.
"""
from __future__ import annotations

import os

# Claude Code writes one record per live session at ``~/.claude/sessions/<repl-pid>.json``.
# Existence alone is enough here: this only decides which ancestor to *name*, and
# reflection re-reads the record and checks its ``procStart`` against the live
# process before injecting anywhere (``session_target.read_session_record``). So a
# stale record squatting a recycled pid fails closed there rather than being
# mis-targeted here, and this stays a narrowing step, not an authority.
SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
MAX_ANCESTRY_HOPS = 8


def _ppid_of(pid: int) -> int | None:
    """``/proc/<pid>/stat`` field 4 (ppid), or ``None`` if the pid is gone.

    Field 2 (comm) is parenthesised and may itself contain spaces and parens, so
    everything up to the *last* ``)`` is skipped; after it, field N sits at index
    N-3.
    """
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fp:
            data = fp.read()
    except OSError:
        return None
    try:
        return int(data[data.rfind(")") + 2:].split()[1])
    except (IndexError, ValueError):
        return None


def resolve_caller_pid(start_pid: int, *, sessions_dir: str = SESSIONS_DIR,
                       ppid_of=_ppid_of,
                       max_hops: int = MAX_ANCESTRY_HOPS) -> int:
    """The nearest ancestor that is a Claude Code session — usually our parent.

    The proxy is normally a direct stdio child of the REPL, so hop zero hits and
    this costs one ``exists``. That stops holding the moment something wraps the
    launch: an ``.mcp.json`` spelling the command ``mamba run -n awm awm-mcp``
    instead of the absolute interpreter path leaves the wrapper sitting between us
    and the REPL, and naming the wrapper made reflection refuse with "the caller
    does not look like a Claude Code session" — a session losing self-compaction
    because of how its config spells a command, with nothing in the message
    pointing at the cause.

    Walking up fixes that without widening what a model can reach. The chain comes
    from ``/proc`` and never from an argument, and the walk stops at the *first*
    session it meets, so an agent nested inside another resolves to its own REPL
    and never to its parent's. A chain containing no session returns ``start_pid``
    unchanged, so a genuine non-Claude caller refuses exactly as it did before.
    """
    pid: int | None = start_pid
    for _ in range(max_hops):
        if pid is None or pid <= 1:
            break
        if os.path.exists(os.path.join(sessions_dir, f"{pid}.json")):
            return pid
        pid = ppid_of(pid)
    return start_pid
