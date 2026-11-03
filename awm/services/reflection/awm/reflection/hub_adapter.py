"""Hub adapter for reflection, the one agent-facing session tool (`install.md` R135).

reflection holds what holds for every harness: who is calling, trust, the R9
session id, the ledger of owed follow-ups and the alarm. Each verb's work
happens in the harness plugin of the session it acts on, called through the
gateway (:mod:`awm.reflection.harness`). The verbs and their published names are
the ones the reflection, cx and keep services served (:mod:`awm.reflection.surface`).

The gateway stamps the caller's pid on every reflection call as ``_caller_pid``.
reflection passes it to the harness plugin as ``pid``, which names the caller's
own session when a verb names no other.

Run via ``run.sh`` (which the hub spawns and respawns):
    python -m awm.reflection.hub_adapter
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import threading
import time
from typing import Any, Optional

from awm.gatewayclient import ServiceAdapter, ids
from awm.reflection import (alarm, elsewhere, harness, ledger, policy, surface,
                             waits)

log = logging.getLogger("awm.reflection.hub_adapter")

API_MANIFEST: dict[str, Any] = {
    "description": surface.DESCRIPTION,
    "functions": surface.FUNCTIONS,
    "emitters": [],
    "sessions": [],
}

SWEEP_EVERY_S = 300


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
    """The calling session's pid, stamped by the gateway. Never a model-supplied value."""
    return args.get("_caller_pid")


def _job(args: dict) -> Optional[str]:
    return args.get("job") or None


def _guarded(verb: str, fn):
    """Turn every failure into ``ok: false`` with a reason, and log it once."""
    def failed(args: dict, exc: Exception) -> dict:
        reason = str(exc)
        if isinstance(exc, harness.UnknownSession) and _caller_pid(args):
            # Only a session's own call looks further: a peer's call has no
            # caller pid, and must not fan out again from here.
            reason += elsewhere.hint(exc.ref)
        log.warning("reflection: %s failed for caller pid %s: %s",
                    verb, _caller_pid(args), reason)
        return {"ok": False, "error": reason}

    def logged(args: dict, result: Any) -> Any:
        if isinstance(result, dict) and not result.get("ok", True):
            # Never log the rest of `args`: `text` is the caller's own prompt.
            log.info("reflection: %s refused for caller pid %s: %s", verb,
                     _caller_pid(args), result.get("reason") or result.get("error"))
        return result

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def run_async(*a):
            try:
                return logged(a[0], await fn(*a))
            except (harness.HarnessError, policy.Refused) as exc:
                return failed(a[0], exc)
        return run_async

    @functools.wraps(fn)
    def run(*a):
        try:
            return logged(a[0], fn(*a))
        except (harness.HarnessError, policy.Refused) as exc:
            return failed(a[0], exc)
    return run


# --- the caller's own session -------------------------------------------------


def _own(verb: str, args: dict, extra: dict) -> tuple[harness.Provider, dict, dict]:
    pid = _caller_pid(args)
    p, who = harness.for_caller(pid)
    return p, who, harness.call(p, verb, {"pid": pid, **extra})


def _with_ticket(p: harness.Provider, who: dict, out: dict) -> dict:
    if out.get("ticket"):
        ledger.open_ticket(out["ticket"], session_id=who.get("session_id") or "",
                           tool=p.tool, command=out.get("text") or "",
                           followup=out.get("followup") or "",
                           name=who.get("name") or "", cwd=who.get("cwd"))
    return out


def _message_self(args: dict) -> dict:
    p, who, out = _own("send", args, {
        "text": args["text"], "submit": _bool(args.get("enter"), True),
        "delay_ms": _int(args.get("delay_ms")), "confirm": _bool(args.get("confirm")),
        "followup": args.get("followup")})
    return _with_ticket(p, who, out)


def _compact(args: dict) -> dict:
    p, who, out = _own("compact", args, {"delay_ms": _int(args.get("delay_ms")),
                                         "followup": args.get("followup")})
    return _with_ticket(p, who, out)


def _mode(args: dict) -> dict:
    return _own("mode", args, {"expect_session": args.get("expect_session")})[2]


def _task_of(session_id: str) -> Optional[str]:
    try:
        from awm.gatewayclient import call_sync
        out = call_sync("reporting", "task_for_session", {"session": session_id},
                        timeout=5.0)
    except Exception:  # noqa: BLE001 - a missing reporting service is no task
        return None
    return out.get("task_id") if isinstance(out, dict) else None


def _session_of(args: dict) -> dict:
    p, who = harness.for_caller(_int(args.get("pid")))
    return {**who, "ok": True, "harness": p.tool, "platform": p.code}


def _whoami(args: dict) -> dict:
    p, who = harness.for_caller(_caller_pid(args))
    sid = who.get("session_id") or ""
    return {**who, "ok": True, "harness": p.tool, "platform": p.code,
            "trust": policy.level_of(sid), "task_id": _task_of(sid) if sid else None,
            "session": who.get("name") or sid}


# --- a session named by `job`, or the caller's own ------------------------------


def _actor_id(args: dict) -> Optional[str]:
    pid = _caller_pid(args)
    if not pid:
        return None
    try:
        return harness.for_caller(pid)[1].get("session_id")
    except harness.HarnessError:
        return None


def _on_target(verb: str, steer: Optional[str], args: dict, extra: dict) -> dict:
    p, named = harness.target(_job(args), _caller_pid(args))
    if steer and _job(args):
        policy.check_steer(_actor_id(args), steer, named.get("session_id"))
    return harness.call(p, verb, {**named, **extra})


def _remote_control(args: dict) -> dict:
    return _on_target("remote_control", "remote_control", args,
                      {"on": _bool(args.get("on")), "name": args.get("name") or None})


def _model(args: dict) -> dict:
    return _on_target("set", "model", args, {"model": args.get("model")})


def _effort(args: dict) -> dict:
    return _on_target("set", "effort", args, {"effort": args.get("level")})


def _rename(args: dict) -> dict:
    return _on_target("set", "rename", args, {"name": args.get("name")})


def _interrupt(args: dict) -> dict:
    return _on_target("interrupt", "interrupt", args, {})


def _stop(args: dict) -> dict:
    p, named = harness.target(_job(args), _caller_pid(args))
    if _job(args):
        policy.check_steer(_actor_id(args), "stop", named.get("session_id"))
    out = harness.call(p, "stop", {**named, "keep": False,
                                   "confirm": _bool(args.get("confirm"))})
    return _after_retire(out, named.get("session_id"))


def _after_retire(out: dict, session_id: Optional[str]) -> dict:
    """A retired session wakes its waiters, here and on every domestic peer (R51)."""
    sid = out.get("session_id") or session_id
    if out.get("ok") and isinstance(sid, str) and ids.is_r9(sid):
        out = {**out, "woken": waits.retired(sid, wake=_wake)}
        threading.Thread(target=_tell_peers, args=(sid,), name="reflection-retired",
                         daemon=True).start()
    return out


def _tell_peers(session_id: str) -> None:
    for peer in _peers():
        try:
            from awm import gatewayclient
            gatewayclient.call_peer_sync(peer, "reflection", "retired",
                                         {"session_id": session_id}, timeout=15.0)
        except Exception as exc:  # noqa: BLE001 - that node checks again at its start
            log.warning("reflection: could not tell %s that %s retired: %s",
                        peer, session_id, exc)


def _peers() -> list[str]:
    try:
        return elsewhere._domestic_peers()
    except Exception as exc:  # noqa: BLE001
        log.info("reflection: cannot read the peer book: %s", exc)
        return []


def _link(args: dict) -> dict:
    return _on_target("link", None, args, {})


def _message(args: dict) -> dict:
    if not _job(args):
        return {"ok": False, "reason": "job is required: the session to message"}
    out = _on_target("send", None, args, {"text": args.get("text"),
                                          "confirm": _bool(args.get("confirm"))})
    hint = policy.first_message_hint(_actor_id(args)) if out.get("ok") else None
    return {**out, "hint": hint} if hint else out


def _scopes_call(fn: str, args: dict) -> Optional[dict]:
    """The scopes service's answer, or None when it does not serve ``fn`` yet."""
    try:
        from awm.gatewayclient import call_sync
        out = call_sync("scopes", fn, args, timeout=10.0)
    except Exception as exc:  # noqa: BLE001
        log.info("reflection: scopes %s unavailable: %s", fn, exc)
        return None
    return out if isinstance(out, dict) else None


def _cd(args: dict) -> dict:
    """Move a session, claiming the scope it moves into (sessions lane, scope claims)."""
    pid = _caller_pid(args)
    p, named = harness.target(_job(args), pid)
    if _job(args):
        policy.check_steer(_actor_id(args), "cd", named.get("session_id"))
    before = harness.call(p, "status", named)
    sid = before.get("session_id") or named.get("session_id") or ""
    where = alarm.scope_of(args.get("path"))
    claim: Any = "unavailable"
    warning = None
    if where and sid:
        project, scope = where
        got = _scopes_call("scope_claim", {
            "project": project, "scope": scope, "session_id": sid,
            "pid": before.get("pid"), "job": before.get("job"),
            "terminal": before.get("kind") == "interactive"})
        if got is not None:
            claim = got
            if not got.get("ok", True) and not got.get("claimed"):
                if before.get("kind") != "interactive":
                    return {"ok": False, "claim": got,
                            "reason": got.get("reason") or
                            f"{project}/{scope} is held by {got.get('holder')}"}
                warning = got.get("reason") or f"{project}/{scope} is held"
    out = harness.call(p, "set", {**named, "cwd": args.get("path")})
    old = alarm.scope_of(before.get("cwd"))
    if out.get("ok") and old and old != where and sid and claim != "unavailable":
        _scopes_call("scope_release", {"project": old[0], "scope": old[1],
                                       "session_id": sid})
    out = {**out, "claim": claim}
    if warning:
        out["warning"] = warning
    return out


def _pending(args: dict) -> dict:
    rows = []
    for p, out in harness.everyone("pending", {}):
        rows += [{**r, "harness": p.tool} for r in out.get("pending") or []]
    return {"ok": True, "pending": rows,
            "tickets": ledger.open_tickets(),
            "lost": [{**entry, "state": "lost"} for entry in alarm.recent()]}


# --- lifecycle ----------------------------------------------------------------


#: The level an isolated session starts at when the caller names none (fleet.md R109).
ISOLATED_TRUST = "untrusted"


def _start_provider(args: dict) -> harness.Provider:
    return harness.for_platform(args.get("platform") or ids.PLATFORM_CLAUDE)


async def _start_other(args: dict, as_: str | None = None) -> dict:
    pid = _caller_pid(args)
    actor = await asyncio.to_thread(_actor_id, args)
    if actor:
        policy.check_steer(actor, "start_other")
    requested = args.get("trust") or (ISOLATED_TRUST if args.get("isolated") else None)
    level = policy.start_level(actor, requested)
    p = _start_provider(args)
    fwd = {k: v for k, v in args.items() if not k.startswith("_") and k != "platform"}
    out = await asyncio.to_thread(harness.call, p, "start", {**fwd, "pid": pid},
                                  as_=as_)
    sid = out.get("session_id")
    if out.get("ok") and sid and level:
        name = str(args.get("name") or "")
        origin = f"seat:{name[5:]}" if name.startswith("seat:") else (
            f"session:{actor}" if actor else (as_ or "service"))
        policy.record(sid, level, origin)
        out = {**out, "trust": level}
    return out


async def _list_other(args: dict, as_: str | None = None) -> dict:
    fwd = {k: v for k, v in args.items() if not k.startswith("_")}
    answers = await asyncio.to_thread(harness.everyone, "list", fwd, as_=as_)
    out: dict[str, Any] = {"ok": True, "sessions": []}
    for p, got in answers:
        if got.get("ok") is False:
            out.setdefault("errors", []).append({"harness": p.tool, **got})
            continue
        out["sessions"] += [{**r, "harness": p.tool} for r in got.get("sessions") or []]
        for key in ("pool", "daemon"):
            if key in got:
                out[key] = got[key]
    return out


async def _on_job(verb: str, steer: str, args: dict, as_: str | None,
                  extra: Optional[dict] = None) -> dict:
    job = _job(args)
    if not job:
        return {"ok": False, "reason": "job is required"}
    pid = _caller_pid(args)
    p, named = await asyncio.to_thread(harness.for_ref, job)
    actor = await asyncio.to_thread(_actor_id, args)
    policy.check_steer(actor, steer, named.get("session_id"))
    return await asyncio.to_thread(harness.call, p, verb,
                                   {**named, "pid": pid, **(extra or {})}, as_=as_)


async def _stop_other(args: dict, as_: str | None = None) -> dict:
    job = _job(args)
    named = {}
    if job:
        try:
            named = (await asyncio.to_thread(harness.for_ref, job))[1]
        except harness.HarnessError:
            named = {}
    out = await _on_job("stop", "stop_other", args, as_, {"keep": False})
    return await asyncio.to_thread(_after_retire, out, named.get("session_id"))


async def _resume_other(args: dict, as_: str | None = None) -> dict:
    out = await _on_job("resume", "resume_other", args, as_,
                        {"prompt": args.get("prompt")})
    if out.get("ok"):
        waits.drop_job(_job(args) or "")
        if isinstance(out.get("session_id"), str):
            waits.drop(out["session_id"])
    return out


async def _adopt_other(args: dict, as_: str | None = None) -> dict:
    return await _on_job("adopt", "start_other", args, as_)


def _claude(verb: str, fwd: Optional[list[str]] = None):
    """A verb only cch serves, forwarded with the named arguments."""
    def run(args: dict) -> dict:
        p = harness.for_platform(ids.PLATFORM_CLAUDE)
        return harness.call(p, verb, {k: args.get(k) for k in fwd or []})
    run.__name__ = f"_{verb}"
    return run


def _wait(args: dict) -> dict:
    until = args.get("until")
    if isinstance(until, str):
        until = [u for u in until.replace(",", " ").split() if u]
    if not until:
        return {"ok": False, "reason": "until is required: the session ids to wait for"}
    try:
        until = list(dict.fromkeys(ids.normalise(u) for u in until))
    except ids.BadSessionId as exc:
        return {"ok": False, "reason": f"{exc}; wait takes session ids, as "
                                       f"start_other returns them"}
    pid = _caller_pid(args)
    p, who = harness.for_caller(pid)
    sid = who.get("session_id") or ""
    if sid in until:
        return {"ok": False, "reason": "a session cannot wait on itself"}
    already = set(waits.retired_among(until)) | set(_retired_elsewhere(until))
    if set(until) <= already:
        return {"ok": True, "waiting": False, "until": until,
                "reason": "every session in until has already retired; carry on"}
    job = who.get("job")
    row = waits.add(session_id=sid, until=until, tool=p.tool, job=job,
                    name=who.get("name") or "", cwd=who.get("cwd"), retired=already)
    if not job:
        return {"ok": True, "waiting": True, "until": row["until"],
                "retired": row["retired"], "terminal": True,
                "note": "a terminal session stays open; the wake line is typed in "
                        "when every listed session has retired. End your turn now"}
    out = harness.call(p, "stop", {"ref": job, "pid": pid, "keep": True,
                                   "confirm": True})
    if not out.get("ok"):
        waits.drop(sid)
        return {**out, "waiting": False}
    return {**out, "ok": True, "waiting": True, "until": row["until"],
            "retired": row["retired"]}


def _waits(args: dict) -> dict:
    return {"ok": True, "waits": waits.load()}


def _retired(args: dict) -> dict:
    sid = ids.to_r9(args.get("session_id"))
    if sid is None:
        return {"ok": False, "reason": "session_id is required: an R9 session id"}
    return {"ok": True, "woken": waits.retired(sid, wake=_wake, local=False)}


def _retired_among(args: dict) -> dict:
    asked = [i for i in (ids.to_r9(s) for s in args.get("session_ids") or []) if i]
    gone = set(waits.retired_among(asked))
    known = [s for s in asked if s not in gone and _session_state(s) in ("live", "stopped")]
    return {"ok": True, "retired": sorted(gone), "known": known}


def _ask_peers(session_ids: list[str]) -> list[dict]:
    from awm import gatewayclient

    out = []
    for peer in _peers():
        try:
            reply = gatewayclient.call_peer_sync(peer, "reflection", "retired_among",
                                                 {"session_ids": session_ids},
                                                 timeout=10.0)
        except Exception as exc:  # noqa: BLE001 - a silent peer cannot say
            log.info("reflection: %s did not answer retired_among: %s", peer, exc)
            out.append({"peer": peer, "answered": False})
            continue
        if isinstance(reply, dict):
            out.append({**reply, "peer": peer, "answered": True})
    return out


def _retired_elsewhere(session_ids: list[str]) -> list[str]:
    if not session_ids:
        return []
    return sorted({s for r in _ask_peers(session_ids) for s in r.get("retired") or []})


def _state_anywhere(session_id: str) -> Optional[str]:
    """``live`` or ``stopped`` when some node knows it, ``gone`` when every node said no."""
    here = _session_state(session_id)
    if here in ("live", "stopped"):
        return here
    replies = _ask_peers([session_id])
    if any(session_id in (r.get("known") or []) for r in replies):
        return "stopped"
    if here is None or not all(r.get("answered") for r in replies):
        return None
    return "gone"


def _session_state(session_id: str) -> Optional[str]:
    try:
        p = harness.for_platform(session_id[:2])
        out = harness.call(p, "status", {"session_id": session_id})
    except harness.HarnessError:
        return None
    if out.get("ok") is False:
        return "gone" if out.get("unknown_ref") else None
    return "live" if out.get("live") else "stopped"


def _wake(row: dict) -> dict:
    """Resume a waiter that kept its job, or type the line into a terminal waiter."""
    p = harness.for_platform(row["session_id"][:2])
    text = waits.wake_text(row)
    if row.get("job"):
        return harness.call(p, "resume", {"ref": row["job"], "adopt_lineage": True,
                                          "prompt": text})
    return harness.call(p, "send", {"session_id": row["session_id"], "text": text})


def _harness_event(args: dict) -> dict:
    event = {k: v for k, v in args.items() if not k.startswith("_")}
    return ledger.record(event)


HANDLERS = {verb: _guarded(verb, fn) for verb, fn in {
    "message_self": _message_self,
    "compact": _compact,
    "mode": _mode,
    "remote_control": _remote_control,
    "model": _model,
    "effort": _effort,
    "rename": _rename,
    "cd": _cd,
    "interrupt": _interrupt,
    "stop": _stop,
    "link": _link,
    "message": _message,
    "pending": _pending,
    "whoami": _whoami,
    "session_of": _session_of,
    "start_other": _start_other,
    "list_other": _list_other,
    "stop_other": _stop_other,
    "resume_other": _resume_other,
    "adopt_other": _adopt_other,
    "claim_other": _claude("pool_claim", ["cwd"]),
    "seed_other": _claude("pool_seed"),
    "remove_other": _claude("pool_remove", ["apply"]),
    "daemon_status": _claude("daemon_status"),
    "daemon_ensure": _claude("daemon_ensure", ["timeout"]),
    "daemon_hold": _claude("daemon_hold", ["reason", "seconds"]),
    "daemon_release": _claude("daemon_release"),
    "wait": _wait,
    "waits": _waits,
    "retired": _retired,
    "retired_among": _retired_among,
    "harness_event": _harness_event,
    "revive_pinned": _claude("revive_pinned"),
}.items()}


def _held(tool: str) -> Optional[list[str]]:
    for p in harness.providers():
        if p.tool == tool:
            try:
                out = harness.call(p, "pending", {})
            except harness.HarnessError:
                return None
            return [r.get("ticket") for r in out.get("pending") or []]
    return None


def _sweep_tickets() -> None:
    while True:
        time.sleep(SWEEP_EVERY_S)
        try:
            ledger.sweep(_held)
        except Exception:  # noqa: BLE001 - a bad sweep must not end the loop
            log.exception("reflection: ticket sweep failed")


def _check_waits() -> None:
    try:
        waits.check(state=_state_anywhere, retired_elsewhere=_retired_elsewhere,
                    wake=_wake)
    except Exception:  # noqa: BLE001
        log.exception("reflection: the start check of the waits failed")


def _on_start() -> None:
    threading.Thread(target=_check_waits, name="reflection-waits", daemon=True).start()
    threading.Thread(target=_sweep_tickets, name="reflection-tickets",
                     daemon=True).start()


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    await ServiceAdapter("reflection", API_MANIFEST, HANDLERS,
                         on_start=_on_start).run()


if __name__ == "__main__":
    asyncio.run(main())
