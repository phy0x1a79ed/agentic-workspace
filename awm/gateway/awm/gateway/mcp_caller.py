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

import json
import os

# Claude Code writes one record per live session at ``~/.claude/sessions/<repl-pid>.json``.
# Existence alone is enough here: this only decides which ancestor to *name*, and
# reflection re-reads the record and checks its ``procStart`` against the live
# process before injecting anywhere (``session_target.read_session_record``). So a
# stale record squatting a recycled pid fails closed there rather than being
# mis-targeted here, and this stays a narrowing step, not an authority.
SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
# Verbs a peer serves that act for the calling session. The peer cannot see our
# pid, so a proxy following a redirect names the session from its own record
# under `CLAIM_KEY`. The peer's edge supplies the node, and the peer's gateway
# strips `CLAIM_KEY` from any call that did not come through an edge.
SESSION_CLAIM_VERBS = {
    "social": frozenset({"mount", "unmount"}),
    "tether": frozenset({"invite", "status", "run", "shell", "keys", "resize",
                         "close", "tasks", "send", "cut", "drain"}),
}
CLAIM_KEY = "_caller_session"
# The calling session's trust level and its own `<project>/<scope>`, on every
# call. The gateway stamps them on a local call. The proxy carries them across a
# peer redirect, and the peer's gateway keeps them only on an edge request.
TRUST_KEY = "_caller_trust"
SCOPE_KEY = "_caller_scope"
DEFAULT_TRUST = "trusted"
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


def _is_opencode(pid: int) -> bool:
    """Is this process an opencode REPL, judged by its own exe?

    OpenCode writes no per-pid record file to key the walk on, so the executable
    is the stop signal. The proxy is a direct stdio child of the opencode REPL,
    so hop zero usually hits; a wrapper spelling the command the way Claude's
    ``mamba run`` wrapper does is walked past the same way.
    """
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return False
    return os.path.basename(exe) == "opencode"


def _default_sessions_dir() -> str:
    """Where the gateway's mode gate reads session records, so both agree.

    `SESSIONS_DIR` is the fallback when `claudedaemon` is not importable.
    """
    try:
        from awm.claudedaemon import sessionmode
        return str(sessionmode.sessions_dir())
    except Exception:  # noqa: BLE001
        return SESSIONS_DIR


def resolve_caller_pid(start_pid: int, *, sessions_dir: str | None = None,
                       ppid_of=_ppid_of, is_opencode=_is_opencode,
                       max_hops: int = MAX_ANCESTRY_HOPS) -> int:
    """The nearest ancestor that is an agent session — usually our parent.

    The proxy is normally a direct stdio child of the REPL, so hop zero hits and
    this costs one cheap check. That stops holding the moment something wraps the
    launch: an ``.mcp.json`` spelling the command ``mamba run -n awm awm-mcp``
    instead of the absolute interpreter path leaves the wrapper sitting between us
    and the REPL, and naming the wrapper made reflection refuse with "the caller
    does not look like a Claude Code session" — a session losing self-compaction
    because of how its config spells a command, with nothing in the message
    pointing at the cause.

    Walking up fixes that without widening what a model can reach. The chain comes
    from ``/proc`` and never from an argument, and the walk stops at the *first*
    session it meets, so an agent nested inside another resolves to its own REPL
    and never to its parent's. An agent session is either a Claude Code REPL (a
    record file exists under ``sessions_dir``) or an opencode REPL (the process's
    own exe is ``opencode``). A chain containing neither returns ``start_pid``
    unchanged, so a genuine non-agent caller refuses exactly as it did before.
    """
    sessions_dir = sessions_dir or _default_sessions_dir()
    pid: int | None = start_pid
    for _ in range(max_hops):
        if pid is None or pid <= 1:
            break
        if os.path.exists(os.path.join(sessions_dir, f"{pid}.json")) or \
                is_opencode(pid):
            return pid
        pid = ppid_of(pid)
    return start_pid


def _record(pid: int, sessions_dir: str | None) -> dict | None:
    path = os.path.join(sessions_dir or _default_sessions_dir(), f"{pid}.json")
    try:
        with open(path, encoding="utf-8") as fp:
            rec = json.load(fp)
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def session_claim(pid: int, *, sessions_dir: str | None = None) -> dict | None:
    """``{kind, job, name}`` from the session record of ``pid``, or None."""
    rec = _record(pid, sessions_dir)
    if rec is None:
        return None
    return {"kind": str(rec.get("kind") or ""), "job": str(rec.get("jobId") or ""),
            "name": str(rec.get("name") or "")}


def _r9(rec: dict) -> str | None:
    """The R9 id of a Claude Code record: ``cc`` and its session UUID without dashes."""
    digits = str(rec.get("sessionId") or "").strip().lower().replace("-", "")
    if len(digits) != 32 or any(c not in "0123456789abcdef" for c in digits):
        return None
    return "cc" + digits


def _default_ledger() -> str:
    from awm.config import SERVICES_DIR
    return str(SERVICES_DIR / "reflection" / "trust.json")


def _default_projects() -> str:
    from awm.config import PROJECTS_DIR
    return str(PROJECTS_DIR)


def _ledger_level(path: str, session_id: str | None) -> str:
    if not session_id:
        return DEFAULT_TRUST
    try:
        with open(path, encoding="utf-8") as fp:
            data = json.load(fp)
        entry = data.get(session_id) if isinstance(data, dict) else None
    except (OSError, ValueError):
        return DEFAULT_TRUST
    level = entry.get("level") if isinstance(entry, dict) else None
    return str(level) if level else DEFAULT_TRUST


def caller_trust(pid: int, *, sessions_dir: str | None = None, ledger: str | None = None,
                 projects: str | None = None) -> dict:
    """``{TRUST_KEY, SCOPE_KEY?}`` for the session ``pid``.

    The level is reflection's ledger entry for the session's R9 id; a session
    with no entry, or no record, is trusted, as the ledger reads it. The scope is
    ``<project>/<scope>`` of the record's ``cwd``, absent outside a project.
    """
    rec = _record(pid, sessions_dir) or {}
    out = {TRUST_KEY: _ledger_level(ledger or _default_ledger(), _r9(rec))}
    if rec.get("cwd"):
        from awm.config import scope_of
        project, scope = scope_of(rec["cwd"], projects or _default_projects())
        if project and scope:
            out[SCOPE_KEY] = f"{project}/{scope}"
    return out


def forwarded_args(name: str, arguments: dict, session_pid: str | int | None, *,
                   sessions_dir: str | None = None, ledger: str | None = None,
                   projects: str | None = None) -> dict:
    """The arguments a proxy sends to the peer a redirect named.

    ``peer`` is dropped so the far side does not re-route. The model's own
    `TRUST_KEY` and `SCOPE_KEY` are replaced by the calling session's, and for a
    verb in `SESSION_CLAIM_VERBS` its own `CLAIM_KEY` by the claim read from the
    calling session's record.
    """
    forwarded = {k: v for k, v in arguments.items() if k != "peer"}
    domain, flat, verb = name.partition("_")
    if flat:
        bag = forwarded
    else:
        verb = forwarded.get("verb")
        inner = forwarded.get("args")
        bag = dict(inner) if isinstance(inner, dict) else {}
        forwarded["args"] = bag
    for key in (TRUST_KEY, SCOPE_KEY):
        bag.pop(key, None)
    pid = int(session_pid) if str(session_pid or "").isdigit() else None
    if pid is not None:
        bag.update(caller_trust(pid, sessions_dir=sessions_dir, ledger=ledger,
                                projects=projects))
    if domain not in SESSION_CLAIM_VERBS:
        return forwarded
    bag.pop(CLAIM_KEY, None)
    if verb in SESSION_CLAIM_VERBS[domain] and pid is not None:
        claim = session_claim(pid, sessions_dir=sessions_dir)
        if claim:
            bag[CLAIM_KEY] = claim
    return forwarded
