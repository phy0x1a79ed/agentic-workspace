"""Starting, listing and stopping sessions on behalf of other agents.

The pool keeps one warm session for a terminal. This module is the other half
of the service: an agent asks for a named session in a scope, and later asks
what is running or to stop one it started.

A started session is not a pool session. It never carries the pool's name
prefix, so nothing in the pool's removal predicates can collect it.

A session's mode is how a later gateway gate restricts it, so every path that
cannot establish a mode answers restrictively. A pending record is written
before the launch, so a launch that outlives its timeout still has a declared
mode; `mode_of` answers `"unknown"` when it cannot read what it needs, and
never turns that into "no mode".

Who may start a session is decided here, from the caller identity the gateway
attached. `parent` comes only from the pid the gateway stamped on the call
(`_caller_pid`), never from an argument the model supplied. Each started
session leaves a lineage record under `config.starts_dir()`. `stop` refuses a
job without one, so an agent cannot stop a session cx did not start, and
`mode_of` reads the session's mode from the same record.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from awm import gatewayclient
from awm.claudedaemon import launch, roster
from awm.config import caller_peer, node_name, node_role, peer_relation

from awm.cx import config, sessions, trust

log = logging.getLogger("awm.cx.lifecycle")

PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual",
                    "dontAsk", "plan")
EFFORTS = ("low", "medium", "high", "xhigh", "max")

#: Default mode of a started session. The representative's gate keys on its own
#: mode name; every other session is a plain worker.
DEFAULT_MODE = "worker"

UNKNOWN = "unknown"

STOP_TIMEOUT_S = 20.0
SCOPE_CREATE_TIMEOUT_S = 1700.0
#: A lineage record younger than this is never pruned, so a record written the
#: instant a job registers cannot be collected before the roster shows it.
PRUNE_GRACE_S = 120.0
#: How long a pending record waits for its late job before it is dropped.
PENDING_TTL_S = 300.0

_PATH_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\[\]:-]*$")
_MODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
_RC_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_JOB = re.compile(r"^[0-9a-f]{8}$")

#: Serialises the name check and the launch, so two starts of one name cannot
#: both pass the check.
_START_LOCK = asyncio.Lock()


class Refused(Exception):
    """A start or stop that must not proceed; the message is the reason."""


# --- who is asking -----------------------------------------------------------


def caller_refusal(as_: str | None) -> str | None:
    """Why this caller may not start or stop sessions, or None if it may.

    A peer caller must be a verified node whose relation is domestic. The bare
    legacy `peer` carries no node name, so it cannot be verified and is refused.
    """
    if as_ is None or not (as_ == "peer" or as_.startswith("peer:")):
        return None
    node = caller_peer(as_)
    if node is None:
        return "an unverified peer call cannot start or stop sessions"
    relation = peer_relation(node)
    if relation != "domestic":
        return (f"peer {node!r} is {relation or 'not in the peer book'}; only a "
                "domestic peer may start or stop sessions")
    return None


def station_refusal() -> str | None:
    try:
        role = node_role()
    except ValueError as exc:
        return f"the node role is invalid: {exc}"
    if role == "station":
        return "this node is a station and does not run sessions for others"
    return None


# --- start -------------------------------------------------------------------


async def start(args: dict[str, Any], as_: str | None = None) -> dict[str, Any]:
    """Create a named background session in a scope's worktree."""
    try:
        return await _start(args, as_)
    except Refused as exc:
        log.info("cx: start refused — %s", exc)
        return {"ok": False, "reason": str(exc)}
    except OSError as exc:
        log.warning("cx: start failed — %s", exc)
        return {"ok": False, "reason": f"start failed: {exc}"}


async def _start(args: dict[str, Any], as_: str | None) -> dict[str, Any]:
    reason = station_refusal() or caller_refusal(as_)
    if reason:
        raise Refused(reason)
    spec = _parse(args)
    ensure_starts_dir()
    _check_caller_mode(args.get("_caller_pid"), spec["mode"])
    if sessions.daemon_pid() is None:
        raise Refused("no claude code daemon is running — a session started now "
                      "would land inside awm's control group")
    cwd = await _resolve_worktree(spec["project"], spec["scope"])
    if not trust.trusted(cwd):
        raise Refused(f"{cwd} is not a trusted directory; trust it once in a "
                      "terminal first")
    parent = _parent_of(args.get("_caller_pid"))
    caller = caller_peer(as_) or as_ or "local"
    async with _START_LOCK:
        if _name_taken(spec["name"]):
            raise Refused(f"a live session is already named {spec['name']!r}")
        record = {
            "name": spec["name"], "project": spec["project"], "scope": spec["scope"],
            "cwd": str(cwd), "parent": parent, "caller": caller,
            "mode": spec["mode"], "remote_control": spec["remote_control"],
            "model": spec["model"], "started_at": _iso(time.time()),
            "started_epoch": time.time(),
        }
        _write_pending(record)
        try:
            session = await launch.launch(
                cwd=cwd, name=spec["name"], flags=build_flags(spec), env={},
                prompt=spec["prompt"], claude=config.claude_bin(),
                unit_prefix="awm-cx-start", roster_path=config.roster_path(),
                jobs_dir=config.jobs_dir(),
                accept=lambda s: spec["name"] in (s.seed_name, s.name))
        except launch.Refused as exc:
            _drop_pending(spec["name"])
            raise Refused(str(exc)) from exc
        except (TimeoutError, OSError) as exc:
            # The pending record stays: the job may still appear, and until it
            # is adopted the declared mode applies to it by name.
            raise Refused(f"the session did not start: {exc}") from exc
        try:
            _write_lineage(session.short, {**record, "job": session.short})
        except OSError as exc:
            raise Refused(f"started {session.short} but could not record it "
                          f"({exc}); the pending record keeps its mode") from exc
        _drop_pending(spec["name"])
    log.info("cx: started %s as %s in %s (mode %s)", spec["name"], session.short,
             cwd, spec["mode"])
    return {"ok": True, "job": session.short, "name": spec["name"],
            "node": node_name(), "project": spec["project"], "scope": spec["scope"],
            "cwd": str(cwd), "attach": f"claude attach {session.short}",
            "parent": parent, "mode": spec["mode"]}


def _check_caller_mode(caller_pid: Any, new_mode: str) -> None:
    """A restricted caller may start only plain workers, and an unknown one nothing.

    No `_caller_pid` means an operator or a service rather than a session, which
    carries no mode. A caller in `worker` mode is unrestricted.
    """
    if caller_pid is None:
        return
    mode = mode_of(caller_pid)
    if mode == UNKNOWN:
        raise Refused("the caller's mode could not be determined, so it may not "
                      "start sessions")
    if mode not in (None, DEFAULT_MODE) and new_mode != DEFAULT_MODE:
        raise Refused(f"a session in mode {mode!r} may start only {DEFAULT_MODE!r} "
                      "sessions")


def _parse(args: dict[str, Any]) -> dict[str, Any]:
    """Validate the arguments and fill in cx's defaults."""
    project, scope = args.get("project"), args.get("scope")
    if not isinstance(project, str) or not _PATH_PART.match(project):
        raise Refused("project must be a plain project name")
    if (not isinstance(scope, str)
            or not all(_PATH_PART.match(part) for part in scope.split("/"))):
        raise Refused("scope must be a scope name (nested scopes use '/')")
    name = args.get("name") or scope
    if not isinstance(name, str):
        raise Refused("name must be a string")
    name = name.strip()
    if not name or len(name) > 80 or not name.isprintable() or name.startswith("-"):
        raise Refused("name must be a short printable string that does not "
                      "start with '-'")
    pool_marks = {"<warm", config.name_prefix().strip().lower()} - {""}
    if any(mark in name.lower() for mark in pool_marks):
        raise Refused("a name may not carry the pool's prefix")
    prompt = args.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        raise Refused("prompt must be a string")
    model = args.get("model") or config.default_model()
    if not isinstance(model, str) or not _MODEL.match(model):
        raise Refused("model is not a model name")
    effort = args.get("effort") or config.default_effort()
    if effort not in EFFORTS:
        raise Refused(f"effort must be one of {', '.join(EFFORTS)}")
    permission = args.get("permission")
    if permission is not None and permission not in PERMISSION_MODES:
        raise Refused(f"permission must be one of {', '.join(PERMISSION_MODES)}")
    mode = args.get("mode") or DEFAULT_MODE
    if not isinstance(mode, str) or not _MODE.match(mode):
        raise Refused("mode must be a short label")
    return {
        "project": project, "scope": scope, "name": name,
        "prompt": prompt or None, "model": model, "effort": effort,
        "permission": permission, "mode": mode,
        "disallowed_tools": _tool_list(args.get("disallowed_tools")),
        "remote_control": _remote_control(args.get("remote_control"), name),
    }


def _tool_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    items = value.split(",") if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(t, str) for t in items):
        raise Refused("disallowed_tools must be a list of tool names")
    return [t.strip() for t in items if t.strip()]


def _remote_control(value: Any, name: str) -> str | None:
    """The Remote Control name to use, or None when it is off.

    The flag takes an optional value, and the CLI reads a value that starts with
    `-` as a new flag, so a name is held to a plain label. `true` derives one
    from the session name.
    """
    if value is None or value is False:
        return None
    if value is True:
        derived = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-._")[:64]
        if not _RC_NAME.match(derived):
            raise Refused("remote_control needs a name; pass one explicitly")
        return derived
    if isinstance(value, str) and _RC_NAME.match(value.strip()):
        return value.strip()
    raise Refused("remote_control must be true, false or a plain label")


def build_flags(spec: dict[str, Any]) -> list[str]:
    """The claude flags for a start, every valued flag in its `=` form.

    The `=` form keeps a value that starts with `-` from being read as a flag.
    The model is a flag, not `ANTHROPIC_MODEL`: the flag wins where the two
    disagree, and it is the only per-session setting that is stated in the
    session's own launch record.
    """
    flags = ([f"--permission-mode={spec['permission']}"] if spec["permission"]
             else config.skip_permission_flags())
    if spec["disallowed_tools"]:
        flags.append("--disallowedTools=" + ",".join(spec["disallowed_tools"]))
    if spec["remote_control"]:
        flags.append(f"--remote-control={spec['remote_control']}")
    return [*flags, f"--effort={spec['effort']}", f"--model={spec['model']}"]


async def _resolve_worktree(project: str, scope: str) -> Path:
    """The scope's worktree, created through the scopes service when absent."""
    root = config.projects_dir()
    cwd = root / project / scope
    if not cwd.resolve().is_relative_to(root.resolve()):
        raise Refused("the scope resolves outside the projects directory")
    if cwd.is_dir():
        return cwd
    try:
        await create_scope(project, scope)
    except Exception as exc:  # noqa: BLE001 — any failure is the reason
        raise Refused(f"could not create scope {project}/{scope}: {exc}") from exc
    if not cwd.is_dir():
        raise Refused(f"scope {project}/{scope} was created but {cwd} is missing")
    return cwd


async def create_scope(project: str, scope: str) -> None:
    await gatewayclient.call("scopes", "scope_create",
                             {"project": project, "scope": scope},
                             timeout=SCOPE_CREATE_TIMEOUT_S)


def _name_taken(name: str) -> bool:
    if any(s.has_record and s.name == name and sessions.is_alive(s)
           for s in sessions.load()):
        return True
    if any(p.get("name") == name for _, p in _pending_records()):
        return True
    return any(rec.get("name") == name for rec in _interactive_records())


# --- lineage -----------------------------------------------------------------


class _Unreadable(Exception):
    """A record `mode_of` needs is missing or unreadable."""


def ensure_starts_dir() -> None:
    try:
        config.starts_dir().mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise Refused(f"cannot keep lineage records ({exc}); refusing to start "
                      "a session that could not be recorded") from exc


def _lineage_path(job: str) -> Path:
    return config.starts_dir() / f"{job}.json"


def _pending_path(name: str) -> Path:
    digest = hashlib.sha256(name.encode()).hexdigest()[:16]
    return config.starts_dir() / f"pending-{digest}.json"


def read_lineage(job: str) -> dict[str, Any] | None:
    if not _JOB.match(job or ""):
        return None
    try:
        data = json.loads(_lineage_path(job).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=1))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _write_lineage(job: str, record: dict[str, Any]) -> None:
    _write_json(_lineage_path(job), record)


def _write_pending(record: dict[str, Any]) -> None:
    """Record the declared mode before the launch; no record, no launch."""
    try:
        _write_json(_pending_path(record["name"]), {**record, "pending": True})
    except OSError as exc:
        raise Refused(f"cannot keep lineage records ({exc}); refusing to start "
                      "a session that could not be recorded") from exc


def _drop_pending(name: str) -> None:
    try:
        _pending_path(name).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("cx: could not drop the pending record of %r: %s", name, exc)


def _lineage_records() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    try:
        names = os.listdir(config.starts_dir())
    except OSError:
        return out
    for fname in names:
        if fname.endswith(".json"):
            rec = read_lineage(fname[:-5])
            if rec:
                out[fname[:-5]] = rec
    return out


def _pending_records() -> list[tuple[Path, dict[str, Any]]]:
    out = []
    try:
        names = os.listdir(config.starts_dir())
    except OSError:
        return out
    for fname in names:
        if fname.startswith("pending-") and fname.endswith(".json"):
            path = config.starts_dir() / fname
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and isinstance(data.get("name"), str):
                out.append((path, data))
    return out


def prune_lineage(now: float | None = None) -> list[str]:
    """Delete lineage records of jobs that `claude rm` has removed.

    A stopped job keeps its record: `claude attach` brings the conversation
    back, and it must come back in the same mode. The job is gone only when the
    roster no longer holds it and its state record is deleted. Nothing is pruned
    when the roster or the jobs directory cannot be read, because every record
    would then look orphaned.
    """
    try:
        workers = roster.read_json_strict(config.roster_path()).get("workers")
    except roster.Unreadable:
        return []
    jobs = config.jobs_dir()
    if not isinstance(workers, dict) or not jobs.is_dir():
        return []
    now = time.time() if now is None else now
    gone = []
    for job in _lineage_records():
        path = _lineage_path(job)
        if job in workers:
            continue
        try:
            if (jobs / job / "state.json").exists():
                continue
            if now - path.stat().st_mtime < PRUNE_GRACE_S:
                continue
            path.unlink()
        except OSError:
            continue
        gone.append(job)
    return gone


def adopt_pending(now: float | None = None) -> list[str]:
    """Give a pending record to the job that outlived its launch timeout.

    A pending record whose job never appeared is dropped after `PENDING_TTL_S`.
    A start in flight holds the start lock and is left to finish.
    """
    if _START_LOCK.locked():
        return []
    pending = _pending_records()
    if not pending:
        return []
    now = time.time() if now is None else now
    held = _lineage_records()
    live = sessions.load()
    adopted = []
    for path, rec in pending:
        started = rec.get("started_epoch")
        started = started if isinstance(started, (int, float)) else 0.0
        late = next((s for s in live
                     if rec["name"] in (s.seed_name, s.name) and s.short not in held
                     and (s.started_at_ms or 0) / 1000.0 >= started - 5.0), None)
        try:
            if late is not None:
                _write_lineage(late.short, {**rec, "job": late.short})
                path.unlink()
                adopted.append(late.short)
                log.info("cx: adopted %s for the pending start of %r",
                         late.short, rec["name"])
            elif now - started > PENDING_TTL_S:
                path.unlink()
        except OSError as exc:
            log.warning("cx: pending record %s: %s", path.name, exc)
    return adopted


def mode_of(caller_pid: int) -> str | None:
    """The mode of the session whose REPL is `caller_pid`.

    Three answers, and a gate must treat them differently:

    - a mode string: `caller_pid` is a session cx started (or is about to adopt),
      and this is the mode it was declared with;
    - `None`: `caller_pid` is positively not a cx-started session. It is an
      interactive terminal, a pool session, or a hand-started job, and the roster
      and the session's own record both say so;
    - `"unknown"`: anything else, including an invalid pid, an unreadable or
      missing roster, jobs directory, session record or lineage record, and a
      background session with no record of how it was started. Treat it as the
      most restricted mode.
    """
    if not isinstance(caller_pid, int) or isinstance(caller_pid, bool) or caller_pid <= 0:
        return UNKNOWN
    try:
        return _mode_of(caller_pid)
    except (_Unreadable, roster.Unreadable, OSError, ValueError, TypeError):
        return UNKNOWN


def _mode_of(pid: int) -> str | None:
    workers = roster.read_json_strict(config.roster_path()).get("workers")
    if (not isinstance(workers, dict) or not config.jobs_dir().is_dir()
            or not config.sessions_dir().is_dir()):
        raise _Unreadable("roster, jobs directory or sessions directory missing")
    lineage, pending = _read_starts()
    for short, w in workers.items():
        if (isinstance(w, dict) and w.get("replPid") == pid
                and roster.proc_start(pid) == str(w.get("replProcStart"))):
            seed = ((w.get("dispatch") or {}).get("seed") or {}).get("name")
            state = roster.read_json(config.jobs_dir() / short / "state.json")
            return _declared_mode(short, {seed, state.get("name")}, lineage, pending)
    rec = _session_record(pid)
    if rec is None or rec.get("kind") != "bg":
        return None
    mode = _declared_mode(str(rec.get("jobId") or ""), {rec.get("name")},
                          lineage, pending)
    return UNKNOWN if mode is None else mode


def _declared_mode(job: str, names: set, lineage: dict[str, dict],
                   pending: list[dict]) -> str | None:
    """The mode recorded for this job, else for a pending start of its name."""
    rec = lineage.get(job)
    if rec is None:
        rec = next((p for p in pending if p.get("name") in names), None)
    if rec is None:
        return None
    mode = rec.get("mode")
    if not isinstance(mode, str) or not _MODE.match(mode):
        raise _Unreadable(f"the record of {job} carries no valid mode")
    return mode


def _read_starts() -> tuple[dict[str, dict], list[dict]]:
    """Every lineage and pending record, raising if any is unreadable."""
    lineage: dict[str, dict] = {}
    pending: list[dict] = []
    directory = config.starts_dir()
    if not directory.is_dir():
        raise _Unreadable("the lineage directory is missing")
    for fname in os.listdir(directory):
        if not fname.endswith(".json"):
            continue
        data = json.loads((directory / fname).read_text())
        if not isinstance(data, dict):
            raise _Unreadable(f"{fname} is not an object")
        if fname.startswith("pending-"):
            pending.append(data)
        elif _JOB.match(fname[:-5]):
            lineage[fname[:-5]] = data
    return lineage, pending


def _session_record(pid: int) -> dict[str, Any] | None:
    """Claude Code's record of the live process `pid`; None when it has none."""
    directory = config.sessions_dir()
    if not directory.is_dir():
        raise _Unreadable("the sessions directory is missing")
    path = directory / f"{pid}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise _Unreadable(f"{path.name} is not an object")
    live = roster.proc_start(pid)
    claimed = str(data.get("procStart", ""))
    if live is None or (claimed and claimed != live):
        raise _Unreadable(f"the record of pid {pid} is stale")
    return data


def _parent_of(pid: Any) -> str | None:
    """The calling session: its job id when a background job, else its session id.

    Taken from the session's own record after checking that the record belongs to
    the live process, so a stale record cannot name a parent.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    rec = roster.session_by_pid(pid, sessions_dir=config.sessions_dir())
    if not rec:
        return None
    if rec.get("kind") == "bg" and rec.get("jobId"):
        return str(rec["jobId"])
    return str(rec["sessionId"]) if rec.get("sessionId") else None


# --- stop --------------------------------------------------------------------


async def stop(args: dict[str, Any], as_: str | None = None) -> dict[str, Any]:
    """Stop a session `start` created. Its conversation is kept."""
    reason = caller_refusal(as_)
    job = args.get("job")
    if not reason and (not isinstance(job, str) or not _JOB.match(job)):
        reason = "job must be a job id"
    rec = None if reason else read_lineage(job)
    if not reason and rec is None:
        reason = f"job {job} was not started by cx start, so cx will not stop it"
    if not reason:
        reason = _stop_caller_refusal(args.get("_caller_pid"), rec)
    if reason:
        log.info("cx: stop refused — %s", reason)
        return {"ok": False, "reason": reason}
    ok, detail = await run_claude_stop(job)
    if not ok:
        return {"ok": False, "job": job, "reason": detail}
    return {"ok": True, "job": job}


def _stop_caller_refusal(caller_pid: Any, rec: dict[str, Any]) -> str | None:
    """A session with a cx mode may stop only the jobs it started itself.

    No `_caller_pid`, or a pid that is positively not a cx session, is an
    operator or a service and may stop any job cx started.
    """
    if caller_pid is None:
        return None
    mode = mode_of(caller_pid)
    if mode is None:
        return None
    if mode == UNKNOWN:
        return "the caller's mode could not be determined, so it may not stop sessions"
    parent = _parent_of(caller_pid)
    if parent is not None and parent == rec.get("parent"):
        return None
    return "a session may stop only the jobs it started"


async def run_claude_stop(job: str) -> tuple[bool, str]:
    proc = await asyncio.create_subprocess_exec(
        config.claude_bin(), "stop", job,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=STOP_TIMEOUT_S)
    except (TimeoutError, asyncio.TimeoutError):
        proc.kill()
        return False, "claude stop timed out"
    text = (out or b"").decode(errors="replace").strip()
    return proc.returncode == 0, text[:300]


# --- list --------------------------------------------------------------------


def collect(project: str | None = None, scope: str | None = None,
            now: float | None = None) -> list[dict[str, Any]]:
    """Every live session on this node, oldest first.

    Background sessions come from the roster, their state records and the
    lineage records; interactive ones from Claude Code's per-process records,
    with the tmux session each one sits in.
    """
    lineage = _lineage_records()
    node = node_name()
    rows = [_job_row(s, lineage.get(s.short), node) for s in sessions.load()
            if s.has_record]
    panes = _tmux_panes()
    rows += [_interactive_row(rec, panes, node) for rec in _interactive_records()]
    out = [r for r in rows
           if (project is None or r["project"] == project)
           and (scope is None or r["scope"] == scope)]
    out.sort(key=lambda r: r["started_at"] or "")
    return out


def _job_row(s: sessions.Session, rec: dict[str, Any] | None,
             node: str) -> dict[str, Any]:
    rec = rec or {}
    project, scope = _project_scope(s.cwd)
    state = _read_json(config.jobs_dir() / s.short / "state.json").get("state")
    return {
        "job": s.short, "tmux": None, "name": s.name, "node": node,
        "project": rec.get("project", project), "scope": rec.get("scope", scope),
        "cwd": s.cwd,
        "state": (state or "unknown") if sessions.is_alive(s) else "gone",
        "attach": f"claude attach {s.short}",
        "parent": rec.get("parent"), "caller": rec.get("caller"),
        "mode": rec.get("mode"), "remote_control": rec.get("remote_control"),
        "started_at": rec.get("started_at") or _iso_ms(s.started_at_ms),
        "pool": sessions.is_ours(s) or sessions.was_ours(s),
    }


def _interactive_row(rec: dict[str, Any], panes: dict[int, str],
                     node: str) -> dict[str, Any]:
    project, scope = _project_scope(rec.get("cwd"))
    tmux = _tmux_session_of(int(rec["pid"]), panes)
    return {
        "job": None, "tmux": tmux, "name": rec.get("name"), "node": node,
        "project": project, "scope": scope, "cwd": rec.get("cwd"),
        "state": rec.get("status") or "unknown",
        "attach": f"tmux attach -t {tmux}" if tmux else None,
        "parent": None, "caller": None, "mode": None, "remote_control": None,
        "started_at": _iso_ms(rec.get("startedAt")), "pool": False,
    }


def _project_scope(cwd: str | None) -> tuple[str | None, str | None]:
    """Project and scope from a directory under the projects tree, else None."""
    if not cwd:
        return None, None
    try:
        parts = Path(cwd).resolve().relative_to(config.projects_dir().resolve()).parts
    except (ValueError, OSError):
        return None, None
    if not parts:
        return None, None
    return parts[0], ("/".join(parts[1:]) or None)


def _interactive_records() -> list[dict[str, Any]]:
    """Live interactive Claude Code sessions, from their per-process records."""
    out = []
    try:
        names = os.listdir(config.sessions_dir())
    except OSError:
        return out
    for fname in names:
        if not fname.endswith(".json"):
            continue
        rec = _read_json(config.sessions_dir() / fname)
        pid = rec.get("pid")
        if rec.get("kind") == "bg" or not isinstance(pid, int):
            continue
        if roster.proc_start(pid) != str(rec.get("procStart")):
            continue
        out.append(rec)
    return out


def _tmux_panes() -> dict[int, str]:
    """Pane pid -> tmux session name, for the default tmux server."""
    if not shutil.which("tmux"):
        return {}
    try:
        proc = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_pid}\t#{session_name}"],
            capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return {}
    out: dict[int, str] = {}
    for line in proc.stdout.splitlines():
        pid, _, name = line.partition("\t")
        if pid.isdigit() and name:
            out[int(pid)] = name
    return out


def _tmux_session_of(pid: int, panes: dict[int, str]) -> str | None:
    """Walk up from `pid` to the pane that holds it."""
    for _ in range(64):
        if pid in panes:
            return panes[pid]
        pid = _ppid(pid)
        if pid <= 1:
            return None
    return None


def _ppid(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            fields = fh.read().partition(") ")[2].split()
        return int(fields[1])
    except (OSError, ValueError, IndexError):
        return 0


# --- small helpers -----------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _iso_ms(ms: Any) -> str | None:
    return _iso(ms / 1000.0) if isinstance(ms, (int, float)) and ms else None
