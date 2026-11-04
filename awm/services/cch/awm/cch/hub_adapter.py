"""Hub adapter for cch, the Claude Code harness plugin (`install.md` R135).

cch serves the hidden tool ``harness-cc``, which implements the harness contract
(``docs/redux/drafts/harness-contract.md``). reflection is its only agent-facing
caller. Other services that act on a Claude Code session call it here too.

A verb that acts on a session names it with ``session_id`` (R9) or ``ref`` (a
job id, a name or a session UUID). With neither, it acts on the session whose
pid is ``pid``: reflection passes the caller pid the gateway stamped on its own
call. cch serves the session lifecycle that cx served, the daemon unit that keep
served, and the injection and resume watcher that reflection served.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.cch.hub_adapter
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import os
import threading
import time
from typing import Any, Optional

from awm import claudedaemon
from awm.claudedaemon import pins, roster
from awm.gatewayclient import ServiceAdapter

from awm.cch import (
    config,
    control,
    cx_adapter,
    events,
    inject,
    isolated,
    keep_adapter,
    lifecycle,
    pending,
    permission_mode,
    session_target,
    tmux_inject,
    transcript,
    watcher,
)

log = logging.getLogger("awm.cch.hub_adapter")

TOOL = "harness-cc"
CONTRACT = "harness@1"
OPTIONAL_VERBS = ("mode", "remote_control", "link", "adopt", "revive_pinned",
                  "pending", "probe", "live_session_ids", "caller_of_token", "pool_claim", "pool_seed", "pool_remove",
                  "daemon_status", "daemon_ensure", "daemon_hold", "daemon_release")

_HOSTING = {"tmux": "terminal", "daemon": "background"}

_FAILURES = (session_target.ResolveError, tmux_inject.TmuxError,
             claudedaemon.DaemonError, inject.DeliveryError, isolated.IsolationError)


def _p(name: str, type_: str = "string", description: str = "",
       required: bool = False) -> dict:
    return {"name": name, "type": type_, "required": required,
            "description": description}


_TARGET = [
    _p("session_id", description="The session's R9 id."),
    _p("ref", description="A job id, a session name or a Claude session UUID."),
    _p("pid", "integer", "The caller's own pid. With no session_id or ref the "
       "verb acts on the session at this pid."),
]


def _verb(name: str, description: str, params: list, *,
          timeout: Optional[int] = None) -> dict:
    out = {"name": name, "tool": f"{TOOL}_{name}", "description": description,
           "params": params}
    if timeout:
        out["timeout"] = timeout
    return out


def _cx_params(name: str) -> list:
    for fn in cx_adapter.API_MANIFEST["functions"]:
        if fn["name"] == name:
            return [dict(p) for p in fn["params"]]
    raise KeyError(name)


def _keep_fn(name: str) -> dict:
    for fn in keep_adapter.API_MANIFEST["functions"]:
        if fn["name"] == name:
            return fn
    raise KeyError(name)


API_MANIFEST: dict[str, Any] = {
    "description": (
        "The Claude Code harness: finds, starts, stops, resumes and types into "
        "Claude Code sessions on this node, and watches a command with a "
        "follow-up through to its end. reflection calls it; it is not on MCP."
    ),
    "functions": [
        _verb("platform", "The platform code, name and optional verbs.", []),
        _verb("resolve", "Whether the session at pid is a Claude Code session, "
            "and which.", [_p("pid", "integer", required=True)],
            timeout=30),
        _verb("find", "The sessions a ref names.", [_p("ref", required=True)], timeout=30),
        _verb("status", "A session's live state.", _TARGET,
            timeout=30),
        _verb("list", "Every Claude Code session on this node, with lineage and "
            "the warm pool.", _cx_params("list") + [
                _p("include_stopped", "boolean")]),
        _verb("send", "Type text or a slash command into a session and submit it.",
            _TARGET + [
                _p("text", required=True),
                _p("submit", "boolean", "Press Enter (default true)."),
                _p("confirm", "boolean", "Allow a destructive command."),
                _p("followup", description="For the caller's own slash "
                   "command: the prompt to type once it finishes."),
                _p("delay_ms", "integer"),
            ], timeout=60),
        _verb("interrupt", "Press Escape in a session.", _TARGET, timeout=30),
        _verb("start", "Start a named background session in a scope's worktree.",
            _cx_params("start") + [
                _p("pid", "integer", "The starting session's pid."),
                _p("trust"), _p("task_id"), _p("role"),
                _p("suggest", "boolean"),
                _p("isolated", "boolean", "Run the session in a rootless container "
                   "with a whitelist of awm verbs (fleet.md R109).")],
            timeout=1800),
        _verb("stop", "Stop a session and remove its job. With keep, the job "
            "stays and a resume brings it back: only a wait keeps it.", _TARGET + [
                _p("keep", "boolean"), _p("confirm", "boolean")], timeout=30),
        _verb("resume", "Bring back a stopped session, or a retired one in a new job, "
            "from its transcript.",
            _TARGET + [_p("prompt"), _p("adopt_lineage", "boolean",
                       "Resume a job that start did not create.")], timeout=90),
        _verb("set", "Set a session's model, effort, name or folder.", _TARGET + [
            _p("model"), _p("effort"), _p("name"), _p("cwd"),
            _p("brief", description="With cwd: the line the session gets once the move "
               "applied, such as the new scope's brief: the caller's own in its "
               "follow-up, another session as a message.")],
            timeout=120),
        _verb("compact", "Compact the caller's own session, then type followup.",
            _TARGET + [_p("followup"), _p("delay_ms", "integer")], timeout=60),
        _verb("mode", "Put the caller's own session back into bypass permissions.",
            [_p("pid", "integer"), _p("expect_session")], timeout=60),
        _verb("remote_control", "Turn Remote Control on or off.", _TARGET + [
            _p("on", "boolean", required=True), _p("name")], timeout=60),
        _verb("link", "A session's claude.ai link.", _TARGET,
            timeout=30),
        _verb("adopt", "Take on a job started outside start.",
            _TARGET + _cx_params("adopt"), timeout=30),
        _verb("revive_pinned", "Resume each pinned job whose process is gone.",
            [], timeout=600),
        _verb("pending", "The follow-ups cch still owes.", []),
        _verb("probe", "Whether cch on `node` answers `list` from this node, and its "
            "daemon state. Returns no sessions. A seat keeper on another node asks "
            "this to tell its own lost contact from the node being down.",
            [_p("node", required=True)], timeout=30),
        _verb("live_session_ids", "The Claude session UUIDs a running Claude Code "
            "process holds. Refuses when a record cannot be read, so a caller never "
            "acts on a shorter list than the truth.", [], timeout=30),
        _verb("caller_of_token", "The isolated session that holds a caller token, "
            "and the verbs it may call.", [_p("token", required=True)],
            timeout=10),
        _verb("pool_claim", "Claim the warm session for a folder.",
            _cx_params("claim"), timeout=9),
        _verb("pool_seed", "Seed one warm session.", [], timeout=60),
        _verb("pool_remove", "Plan or apply removal of stale pool sessions.",
            _cx_params("remove"), timeout=120),
        *[_verb(f"daemon_{v}", _keep_fn(v)["description"],
              [dict(p) for p in _keep_fn(v)["params"]],
              timeout=_keep_fn(v).get("timeout"))
          for v in ("status", "ensure", "hold", "release")],
    ],
    "emitters": [],
    "sessions": [],
}


# --- targeting ---------------------------------------------------------------


def _uuid_of(session_id: str) -> str:
    digits = session_id[2:]
    if len(digits) != 32:
        return digits
    return "-".join((digits[:8], digits[8:12], digits[12:16], digits[16:20],
                     digits[20:]))


def _ref(args: dict) -> Optional[str]:
    """The ``job`` control and lifecycle take: a ref, or None for the caller's own."""
    sid = str(args.get("session_id") or "").strip().lower()
    if sid:
        if not sid.startswith(events.PLATFORM):
            raise session_target.ResolveError(
                f"{sid!r} is not a Claude Code session id (it does not start "
                f"with {events.PLATFORM!r})")
        return _uuid_of(sid)
    ref = str(args.get("ref") or "").strip()
    return ref or None


def _pid(args: dict) -> Optional[int]:
    try:
        return int(args["pid"]) if args.get("pid") not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    return v in (True, "true", "True", "1", 1)


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _with_ids(out: dict) -> dict:
    sid = out.get("session_id")
    if isinstance(sid, str) and sid and not sid.startswith(events.PLATFORM):
        out = {**out, "claude_session_id": sid, "session_id": events.r9(sid)}
    return out


def _guarded(verb: str, fn):
    @functools.wraps(fn)
    def run(*a):
        args = a[0]
        try:
            result = fn(*a)
        except control.UnknownSession as exc:
            log.info("cch: %s: %s", verb, exc)
            return {"ok": False, "error": str(exc), "unknown_ref": exc.ref}
        except _FAILURES as exc:
            log.warning("cch: %s failed for pid %s: %s", verb, args.get("pid"), exc)
            return {"ok": False, "error": str(exc)}
        if isinstance(result, dict) and not result.get("ok", True):
            log.info("cch: %s refused for pid %s: %s", verb, args.get("pid"),
                     result.get("reason") or result.get("error"))
        return result

    @functools.wraps(fn)
    async def run_async(*a):
        args = a[0]
        try:
            return await fn(*a)
        except _FAILURES as exc:
            log.warning("cch: %s failed for pid %s: %s", verb, args.get("pid"), exc)
            return {"ok": False, "error": str(exc)}

    return run_async if inspect.iscoroutinefunction(fn) else run


# --- isolated sessions -------------------------------------------------------


def _isolated(args: dict) -> Optional[dict]:
    """The isolated session that ``session_id`` or ``ref`` names, if any."""
    ref = _ref(args)
    return isolated.find(ref) if ref else None


def _iso_live(rec: dict) -> bool:
    try:
        return isolated.running(rec)
    except (isolated.IsolationError, OSError, ValueError):
        return False


def _iso_row(rec: dict, live: Optional[bool] = None) -> dict:
    sid = rec["claude_session_id"]
    return {"session_id": events.r9(sid), "claude_session_id": sid,
            "name": rec.get("name"), "job": None, "pid": None,
            "live": _iso_live(rec) if live is None else live, "kind": "isolated",
            "container": rec.get("container"), "project": rec.get("project"),
            "scope": rec.get("scope"), "cwd": rec.get("cwd")}


async def _start_isolated(args: dict, as_: Optional[str]) -> dict:
    """Start an isolated session (fleet.md R109). It runs in no daemon and has no job."""
    try:
        reason = lifecycle.station_refusal() or lifecycle.caller_refusal(as_)
        if reason:
            raise lifecycle.Refused(reason)
        spec = lifecycle._parse(args)
        cwd = await lifecycle._resolve_worktree(spec["project"], spec["scope"])
        if any(r.get("name") == spec["name"] and _iso_live(r)
               for r in await asyncio.to_thread(isolated.records)):
            raise lifecycle.Refused(f"a live isolated session is already named "
                                    f"{spec['name']!r}")
        rec = await asyncio.to_thread(
            isolated.start, project=spec["project"], scope=spec["scope"],
            worktree=cwd, name=spec["name"], prompt=spec["prompt"] or "",
            model=spec["model"], effort=spec["effort"], claude_bin=config.claude_bin())
    except lifecycle.Refused as exc:
        return lifecycle.refusal(exc)
    except (isolated.IsolationError, OSError) as exc:
        return {"ok": False, "reason": f"isolated start failed: {exc}"}
    return {"ok": True, "isolated": True, "node": lifecycle.node_name(),
            **_iso_row(rec, live=True)}


def _caller_of_token(args: dict) -> dict:
    rec = isolated.caller_of_token(args.get("token"))
    if rec is None:
        return {"ok": True, "known": False}
    return {"ok": True, "known": True, **_iso_row(rec, live=True),
            "trust": rec.get("trust"), "tools": rec.get("tools") or {}}


# --- contract verbs ----------------------------------------------------------


def _platform(args: dict) -> dict:
    return {"ok": True, "code": events.PLATFORM, "name": "Claude Code",
            "contract": CONTRACT, "capabilities": list(OPTIONAL_VERBS)}


def _job_name(job: Optional[str]) -> Optional[str]:
    """A job's own name. The session record keeps the name it was seeded with."""
    if not job:
        return None
    state = roster.read_json(roster.default_jobs_dir() / job / "state.json")
    return str(state.get("name") or "") or None


def _gate_mode(pid: Any) -> Optional[str]:
    """The mode cx started the session at ``pid`` with, for the gateway's gate."""
    from awm.claudedaemon import sessionmode

    if not isinstance(pid, int):
        return sessionmode.UNKNOWN
    try:
        return sessionmode.mode_of(pid)
    except Exception as exc:  # noqa: BLE001 - a mode that cannot be read restricts hardest
        log.warning("cch: could not read the mode of pid %s: %s", pid, exc)
        return sessionmode.UNKNOWN


def _resolve(args: dict) -> dict:
    pid = _pid(args)
    try:
        who = inject.describe_caller(pid)
    except session_target.ResolveError as exc:
        return {"ok": True, "mine": False, "error": str(exc), "mode": _gate_mode(pid)}
    cwd = transcript.session_and_cwd_for(who["pid"])[1]
    job = roster.job_of_pid(who["pid"], sessions_dir=session_target.SESSIONS_DIR)
    return _with_ids({"ok": True, "mine": True, "name": _job_name(job) or who.get("session"),
                      "session_id": who.get("session_id"), "pid": who["pid"],
                      "lane": who.get("hosting"),
                      "hosting": _HOSTING.get(who.get("hosting"), who.get("hosting")),
                      "pane": who.get("pane"),
                      "job": job,
                      "proc_start": roster.proc_start(who["pid"]),
                      "cwd": cwd, "mode": _gate_mode(pid)})


def _row(t: control.Target) -> dict:
    return _with_ids({"session_id": t.session_id, "name": t.name, "job": t.job,
                      "pid": t.pid, "live": t.alive, "kind": t.kind})


def _find(args: dict) -> dict:
    rec = _isolated(args)
    if rec is not None:
        return {"ok": True, "matches": [_iso_row(rec)]}
    ref = _ref(args)
    try:
        t = control.resolve(ref, None)
    except control.UnknownSession:
        return {"ok": True, "matches": []}
    return {"ok": True, "matches": [_row(t)]}


def _status(args: dict) -> dict:
    rec = _isolated(args)
    if rec is not None:
        row = _iso_row(rec)
        return {"ok": True, **row, "state": "running" if row["live"] else "stopped"}
    t = control.resolve(_ref(args), _pid(args))
    status = watcher.read_status(t.pid) if t.alive else None
    state = "stopped" if not t.alive else (status[0] if status else "unknown")
    return {"ok": True, **_row(t), "state": state,
            "cwd": transcript.session_and_cwd_for(t.pid)[1] if t.alive else None}


async def _list(args: dict, as_: str | None = None) -> dict:
    out = await cx_adapter._list(args, as_)
    for row in out.get("sessions") or []:
        sid = row.get("session_id")
        if isinstance(sid, str) and sid and not sid.startswith(events.PLATFORM):
            row["claude_session_id"] = sid
            row["session_id"] = events.r9(sid)
    recs = await asyncio.to_thread(isolated.records)
    if recs:
        rows = await asyncio.to_thread(lambda: [_iso_row(r) for r in recs])
        project, scope = args.get("project"), args.get("scope")
        rows = [r for r in rows if (not project or r["project"] == project)
                and (not scope or r["scope"] == scope)
                and (r["live"] or _bool(args.get("include_stopped")))]
        out.setdefault("sessions", []).extend(rows)
    return out


def _send(args: dict) -> dict:
    rec = _isolated(args)
    if rec is not None:
        return isolated.send(rec, str(args.get("text") or ""),
                             submit=_bool(args.get("submit"), True))
    ref, pid = _ref(args), _pid(args)
    if ref:
        return control.message(ref, args.get("text"), caller_pid=pid,
                               confirm=_bool(args.get("confirm")))
    return inject.send(args["text"], caller_pid=pid,
                       enter=_bool(args.get("submit"), True),
                       delay_ms=_int(args.get("delay_ms")),
                       confirm=_bool(args.get("confirm")),
                       followup=args.get("followup"))


def _interrupt(args: dict) -> dict:
    rec = _isolated(args)
    if rec is not None:
        return isolated.interrupt(rec)
    return control.interrupt(job=_ref(args), caller_pid=_pid(args))


def _lifecycle_args(args: dict) -> dict:
    out = {k: v for k, v in args.items()
           if k not in ("pid", "session_id", "ref", "keep", "confirm")}
    ref = _ref(args)
    if ref:
        out["job"] = ref
    if _pid(args) is not None:
        out["_caller_pid"] = _pid(args)
    return out


async def _start(args: dict, as_: str | None = None) -> dict:
    if _bool(args.get("isolated")):
        return await _start_isolated(args, as_)
    out = await lifecycle.start(_lifecycle_args(args), as_)
    return _with_ids(out) if isinstance(out, dict) else out


def _stop_keeping(job: str) -> dict:
    """Stop ``job`` and keep it, so it resumes by its session id (`sessions.md` R10)."""
    from awm.claudedaemon import cli

    out = cli.stop_keeping(job, claude=config.claude_bin())
    if not out["stopped"] and not cli.JobProbe().worker_alive(job):
        out = {**out, "stopped": True, "detail": "it had already stopped"}
    if out["stopped"]:
        lifecycle._record_stop(job)
    return out


async def _stop(args: dict, as_: str | None = None) -> dict:
    """Stop a session. A job that `start` created goes through its lineage policy."""
    rec = await asyncio.to_thread(_isolated, args)
    if rec is not None:
        # The container goes either way. The transcript stays, so a retired
        # isolated session is still resumable (sessions.md R10).
        return await asyncio.to_thread(isolated.stop, rec, keep=True)
    ref, pid = _ref(args), _pid(args)
    confirm = _bool(args.get("confirm"))
    try:
        t = await asyncio.to_thread(control.resolve, ref, pid)
    except session_target.ResolveError:
        t = None
    if not _bool(args.get("keep")):
        out = await asyncio.to_thread(control.stop, ref, caller_pid=pid, confirm=confirm)
    elif t is not None and t.job and not t.is_self and lifecycle.read_lineage(t.job):
        out = await lifecycle.stop({"job": t.job, "_caller_pid": pid}, as_)
    else:
        out = await asyncio.to_thread(control.stop, ref, caller_pid=pid, confirm=confirm,
                                      remove=_stop_keeping)
    # reflection wakes the sessions that wait on this one by its id (sessions.md R51).
    if isinstance(out, dict) and t is not None and t.session_id and "session_id" not in out:
        out = {**out, "session_id": events.r9(t.session_id)}
    return out


def _live_session_ids(args: dict) -> dict:
    from awm.cch import live
    from awm.claudedaemon import roster

    try:
        return {"ok": True, "session_ids": sorted(live.session_ids())}
    except roster.Unreadable as exc:
        return {"ok": False, "reason": f"unreadable {exc.path}: {exc}"}


def _ensure_lineage(job: str, origin: str, **extra: Any) -> Optional[str]:
    """Give ``job`` a lineage record when it has none, so `resume` takes it. Returns an error."""
    if lifecycle.read_lineage(job) is not None:
        return None
    try:
        lifecycle._write_lineage(job, {"job": job, "parent": None, "origin": origin,
                                       **extra})
    except OSError as exc:
        return f"no lineage: {exc}"
    return None


async def _resume(args: dict, as_: str | None = None) -> dict:
    rec = await asyncio.to_thread(_isolated, args)
    if rec is not None:
        if await asyncio.to_thread(_iso_live, rec):
            return {"ok": False, "reason": f"{rec['name']} is already running"}
        await asyncio.to_thread(isolated.resume, rec, prompt=str(args.get("prompt") or ""))
        return {"ok": True, "resumed": True, **_iso_row(rec, live=True)}
    fwd = _lifecycle_args(args)
    fwd.pop("adopt_lineage", None)
    if _bool(args.get("adopt_lineage")) and fwd.get("job"):
        error = _ensure_lineage(fwd["job"], "wait")
        if error:
            return {"ok": False, "reason": error}
    return await lifecycle.resume(fwd, as_)


def _set(args: dict) -> dict:
    if _isolated(args) is not None:
        return {"ok": False, "unsupported": True,
                "error": "an isolated session keeps the model, effort, name and folder "
                         "it started with"}
    ref, pid = _ref(args), _pid(args)
    actions = {
        "model": lambda v: control.model(v, job=ref, caller_pid=pid),
        "effort": lambda v: control.effort(v, job=ref, caller_pid=pid),
        "name": lambda v: control.rename(v, job=ref, caller_pid=pid),
        "cwd": lambda v: control.cd(v, job=ref, caller_pid=pid, brief=args.get("brief") or ""),
    }
    asked = [k for k in actions if args.get(k)]
    if not asked:
        return {"ok": False, "error": "set needs one of model, effort, name or cwd"}
    applied = {k: actions[k](args[k]) for k in asked}
    out = {"ok": all(r.get("ok") for r in applied.values()), "applied": applied}
    if len(asked) == 1:
        out = {**applied[asked[0]], "applied": applied}
    return out


def _compact(args: dict) -> dict:
    if _ref(args):
        return {"ok": False, "unsupported": True,
                "error": "cch compacts only the caller's own session"}
    return inject.send("/compact", caller_pid=_pid(args),
                       delay_ms=_int(args.get("delay_ms")),
                       followup=args.get("followup"))


# --- optional verbs ----------------------------------------------------------


def _mode(args: dict) -> dict:
    return permission_mode.ensure_bypass(
        caller_pid=_pid(args), expect_session=args.get("expect_session") or None)


def _remote_control(args: dict) -> dict:
    return control.remote_control(_bool(args.get("on")),
                                  name=args.get("name") or None,
                                  job=_ref(args), caller_pid=_pid(args))


def _link(args: dict) -> dict:
    return control.link(job=_ref(args), caller_pid=_pid(args))


async def _adopt(args: dict, as_: str | None = None) -> dict:
    return await lifecycle.adopt(_lifecycle_args(args), as_)


def _live_jobs() -> set[str]:
    return {s.short for s in roster.load(session_target.ROSTER_PATH,
                                         session_target.JOBS_DIR)
            if isinstance(s.repl_pid, int)
            and control._alive(s.repl_pid, s.repl_proc_start)}


async def _revive_pinned(args: dict) -> dict:
    """Resume each pinned job whose process is gone, after a reboot (fleet lane)."""
    live = await asyncio.to_thread(_live_jobs)
    revived, still, failed = [], [], []
    for job in await asyncio.to_thread(pins.pinned):
        if job in live:
            still.append(job)
            continue
        error = _ensure_lineage(job, "revive_pinned", pinned=True)
        if error:
            failed.append({"job": job, "error": error})
            continue
        out = await lifecycle.resume({"job": job})
        if out.get("ok"):
            revived.append(job)
        else:
            failed.append({"job": job, "error": out.get("reason") or out.get("error")})
    return {"ok": not failed, "revived": revived, "live": still, "failed": failed}


BOOT_ID = "/proc/sys/kernel/random/boot_id"
REVIVE_TRIES = 10
REVIVE_WAIT_S = 30.0
_revive_task: Optional[asyncio.Task] = None


def _boot_mark() -> "os.PathLike[str]":
    return config.state_dir() / "pinned-revived-boot"


def _first_start_this_boot() -> bool:
    """Whether cch starts for the first time since the host booted, recording that it has.

    No mark yet is a first install, not a reboot: it records the boot and revives nothing.
    """
    try:
        with open(BOOT_ID) as fp:
            boot = fp.read().strip()
    except OSError:
        return False
    mark = _boot_mark()
    try:
        before = mark.read_text().strip()
    except OSError:
        before = None
    try:
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(boot)
    except OSError as exc:
        log.warning("cch: could not record the boot: %s", exc)
        return False
    return before is not None and before != boot


async def _revive_after_boot() -> None:
    """Bring back the pinned jobs a host reboot ended, once the daemon is up (fleet.md R139)."""
    if not await asyncio.to_thread(_first_start_this_boot):
        return
    for _ in range(REVIVE_TRIES):
        try:
            await lifecycle.ensure_daemon("revived")
        except lifecycle.Refused as exc:
            log.info("cch: waiting for the daemon to revive pinned jobs: %s", exc)
            await asyncio.sleep(REVIVE_WAIT_S)
            continue
        out = await _revive_pinned({})
        log.info("cch: after the reboot, revived %s, still live %s, failed %s",
                 out["revived"], out["live"], out["failed"])
        if out["ok"]:
            return
        await asyncio.sleep(REVIVE_WAIT_S)
    log.error("cch: pinned jobs not all revived after the reboot")


def _pending(args: dict) -> dict:
    now = int(time.time() * 1000)
    return {"ok": True, "pending": [
        {"session": p.name or p.session_id,
         "session_id": events.r9(p.session_id) if p.session_id else "",
         "pid": p.repl_pid, "hosting": p.hosting, "command": p.text,
         "resume": p.followup, "ticket": p.ticket,
         "waiting_for_s": max(0, (now - p.injected_at_ms) // 1000),
         "holds": p.holds, "nudges": p.nudges, "last_outcome": p.last_outcome,
         "phase": p.phase, "reissues": p.reissues, "state": "pending"}
        for p in pending.load_all()]}


HANDLERS = {verb: _guarded(verb, fn) for verb, fn in {
    "platform": _platform,
    "resolve": _resolve,
    "find": _find,
    "status": _status,
    "list": _list,
    "send": _send,
    "interrupt": _interrupt,
    "start": _start,
    "stop": _stop,
    "resume": _resume,
    "set": _set,
    "compact": _compact,
    "mode": _mode,
    "remote_control": _remote_control,
    "link": _link,
    "adopt": _adopt,
    "revive_pinned": _revive_pinned,
    "pending": _pending,
    "probe": cx_adapter._probe,
    "live_session_ids": _live_session_ids,
    "caller_of_token": _caller_of_token,
    "pool_claim": cx_adapter._claim,
    "pool_seed": cx_adapter._seed,
    "pool_remove": cx_adapter._remove,
    "daemon_status": keep_adapter._status,
    "daemon_ensure": keep_adapter._ensure,
    "daemon_hold": keep_adapter._hold,
    "daemon_release": keep_adapter._release,
}.items()}


def _replay() -> None:
    """Re-arm the follow-ups the process this one replaced still owed."""
    try:
        outcome = inject.replay_pending()
    except Exception as exc:  # noqa: BLE001 - a bad record must not keep cch down
        log.warning("cch: could not replay pending resumes: %s", exc)
        return
    if outcome["pending"]:
        log.info("cch: %s pending resume(s) found on boot, %s re-armed",
                 outcome["pending"], outcome["resumed"])
    threading.Thread(target=events.resend_unsent, name="cch-resend",
                     daemon=True).start()


def _on_start() -> None:
    global _revive_task
    _replay()
    keep_adapter._on_start()
    cx_adapter._on_start()
    _revive_task = asyncio.get_running_loop().create_task(_revive_after_boot())


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    await ServiceAdapter("cch", API_MANIFEST, HANDLERS, on_start=_on_start).run()


if __name__ == "__main__":
    asyncio.run(main())
