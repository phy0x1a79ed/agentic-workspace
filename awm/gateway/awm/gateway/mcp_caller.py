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
import threading
import time
import urllib.error
import urllib.request

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


#: How long one answer about a pid stands. The gate, the trust stamp and the
#: proxy walk each ask on every call, so this bounds the lookups per session.
LOOKUP_TTL_S = 5.0
LOOKUP_TIMEOUT_S = 5.0
_LOOKUP_MAX = 256
_cache: dict[int, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


class LookupUnavailable(RuntimeError):
    """reflection did not answer, so nothing about the pid is known."""


def _ask_reflection(pid: int) -> dict:
    """``reflection session_of {pid}`` through the local gateway's ``/svc`` door.

    The call carries no session pid header, so the gateway neither gates nor
    stamps it, and the lookup never asks about itself.
    """
    from awm.config import BASE_URL

    req = urllib.request.Request(
        f"{BASE_URL.rstrip('/')}/svc/reflection/fn/session_of",
        data=json.dumps({"pid": pid}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=LOOKUP_TIMEOUT_S) as resp:
            out = json.loads(resp.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LookupUnavailable(f"reflection session_of: {exc}") from exc
    if not isinstance(out, dict):
        raise LookupUnavailable(f"reflection session_of answered {out!r}")
    return out


def lookup(pid: int) -> dict:
    """What reflection knows of ``pid``: ``known``, and for a session its
    ``session_id``, ``mode``, ``trust``, ``cwd``, ``hosting``, ``job`` and ``name``.
    Remembered for `LOOKUP_TTL_S`. Raises `LookupUnavailable` when reflection
    does not answer, which is never remembered."""
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(pid)
    if hit is not None and now - hit[0] < LOOKUP_TTL_S:
        return hit[1]
    try:
        out = _ask_reflection(pid)
    except LookupUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - any failure is no answer
        raise LookupUnavailable(f"reflection session_of: {exc}") from exc
    rec = {**out, "known": bool(out.get("ok") and out.get("session_id"))}
    with _cache_lock:
        if len(_cache) >= _LOOKUP_MAX:
            _cache.clear()
        _cache[pid] = (now, rec)
    return rec


def resolve_caller_pid(start_pid: int, *, ppid_of=_ppid_of,
                       max_hops: int = MAX_ANCESTRY_HOPS) -> int:
    """The nearest ancestor that is an agent session — usually our parent.

    The proxy is normally a direct stdio child of the REPL, so hop zero hits.
    That stops holding the moment something wraps the launch: an ``.mcp.json``
    spelling the command ``mamba run -n awm awm-mcp`` leaves the wrapper sitting
    between us and the REPL, and naming the wrapper made reflection refuse.

    Walking up fixes that without widening what a model can reach. The chain comes
    from ``/proc`` and never from an argument, and the walk stops at the *first*
    session it meets, so an agent nested inside another resolves to its own REPL
    and never to its parent's. A pid is a session when reflection, through the
    session's harness plugin, knows it. A chain with none, or no answer from
    reflection, returns ``start_pid`` unchanged, so a genuine non-agent caller
    refuses exactly as it did before.
    """
    pid: int | None = start_pid
    for _ in range(max_hops):
        if pid is None or pid <= 1:
            break
        try:
            if lookup(pid)["known"]:
                return pid
        except LookupUnavailable:
            return start_pid
        pid = ppid_of(pid)
    return start_pid


def _known(pid: int) -> dict:
    try:
        rec = lookup(pid)
    except LookupUnavailable:
        return {}
    return rec if rec.get("known") else {}


def session_claim(pid: int) -> dict | None:
    """``{kind, job, name}`` of the session ``pid``, or None."""
    rec = _known(pid)
    if not rec:
        return None
    return {"kind": "bg" if rec.get("hosting") == "background" else "interactive",
            "job": str(rec.get("job") or ""), "name": str(rec.get("name") or "")}


def _default_projects() -> str:
    from awm.config import PROJECTS_DIR
    return str(PROJECTS_DIR)


def caller_trust(pid: int, *, projects: str | None = None) -> dict:
    """``{TRUST_KEY, SCOPE_KEY?}`` for the session ``pid``.

    The level is reflection's for the session; a session reflection holds no
    level for, or no session, is trusted, as reflection's ledger reads it. The
    scope is ``<project>/<scope>`` of the session's ``cwd``, absent outside a
    project.
    """
    rec = _known(pid)
    out = {TRUST_KEY: str(rec.get("trust") or DEFAULT_TRUST)}
    if rec.get("cwd"):
        from awm.config import scope_of
        project, scope = scope_of(rec["cwd"], projects or _default_projects())
        if project and scope:
            out[SCOPE_KEY] = f"{project}/{scope}"
    return out


def forwarded_args(name: str, arguments: dict, session_pid: str | int | None, *,
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
        bag.update(caller_trust(pid, projects=projects))
    if domain not in SESSION_CLAIM_VERBS:
        return forwarded
    bag.pop(CLAIM_KEY, None)
    if verb in SESSION_CLAIM_VERBS[domain] and pid is not None:
        claim = session_claim(pid)
        if claim:
            bag[CLAIM_KEY] = claim
    return forwarded
