"""Starting, listing and stopping sessions on behalf of other agents.

The pool keeps one warm session for a terminal. This module is the other half
of the service: an agent asks for a named session in a scope, and later asks
what is running or to stop one it started.

A started session is not a pool session. It never carries the pool's name
prefix, so nothing in the pool's removal predicates can collect it.

Who may start a session is decided here, from the caller identity the gateway
attached. `parent` comes only from the pid the gateway stamped on the call
(`_caller_pid`), never from an argument the model supplied. Each started
session leaves a lineage record under `config.starts_dir()`. `stop` refuses a
job without one, so an agent cannot stop a session cx did not start, and
`mode_of` reads the session's mode from the same record.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from awm import gatewayclient
from awm.config import caller_peer, node_name, node_role, peer_relation

from awm.cx import config, sessions, trust

log = logging.getLogger("awm.cx.lifecycle")

PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual",
                    "dontAsk", "plan")
EFFORTS = ("low", "medium", "high", "xhigh", "max")

#: Default mode of a started session. The representative's gate keys on its own
#: mode name; every other session is a plain worker.
DEFAULT_MODE = "worker"

LAUNCH_TIMEOUT_S = 45.0
POLL_S = 0.25
STOP_TIMEOUT_S = 20.0
SCOPE_CREATE_TIMEOUT_S = 1700.0
#: A lineage record younger than this is never pruned, so a record written the
#: instant a job registers cannot be collected before the roster shows it.
PRUNE_GRACE_S = 120.0

_PATH_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\[\]:-]*$")
_MODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
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


async def _start(args: dict[str, Any], as_: str | None) -> dict[str, Any]:
    reason = station_refusal() or caller_refusal(as_)
    if reason:
        raise Refused(reason)
    spec = _parse(args)
    if sessions.daemon_pid() is None:
        raise Refused("no claude code daemon is running — a session started now "
                      "would land inside awm's control group")
    cwd = await _resolve_worktree(spec["project"], spec["scope"])
    if not trust.trusted(cwd):
        raise Refused(f"{cwd} is not a trusted directory; trust it once in a "
                      "terminal first")
    async with _START_LOCK:
        if _name_taken(spec["name"]):
            raise Refused(f"a live session is already named {spec['name']!r}")
        flags = build_flags(spec)
        try:
            session = await launch_session(cwd=cwd, name=spec["name"], flags=flags,
                                           prompt=spec["prompt"])
        except (TimeoutError, OSError) as exc:
            raise Refused(f"the session did not start: {exc}") from exc
        parent = _parent_of(args.get("_caller_pid"))
        caller = caller_peer(as_) or as_ or "local"
        _write_lineage(session.short, {
            "job": session.short, "name": spec["name"], "project": spec["project"],
            "scope": spec["scope"], "cwd": str(cwd), "parent": parent,
            "caller": caller, "mode": spec["mode"],
            "remote_control": spec["remote_control"], "model": spec["model"],
            "started_at": _iso(time.time()),
        })
    log.info("cx: started %s as %s in %s (mode %s)", spec["name"], session.short,
             cwd, spec["mode"])
    return {"ok": True, "job": session.short, "name": spec["name"],
            "node": node_name(), "project": spec["project"], "scope": spec["scope"],
            "cwd": str(cwd), "attach": f"claude attach {session.short}",
            "parent": parent, "mode": spec["mode"]}


def _parse(args: dict[str, Any]) -> dict[str, Any]:
    """Validate the arguments and fill in cx's defaults."""
    project, scope = args.get("project"), args.get("scope")
    if not isinstance(project, str) or not _PATH_PART.match(project):
        raise Refused("project must be a plain project name")
    if (not isinstance(scope, str)
            or not all(_PATH_PART.match(part) for part in scope.split("/"))):
        raise Refused("scope must be a scope name (nested scopes use '/')")
    name = args.get("name") or scope
    if (not isinstance(name, str) or not name.strip() or len(name) > 80
            or not name.isprintable()):
        raise Refused("name must be a short printable string")
    if name.startswith(config.name_prefix()):
        raise Refused(f"a name may not start with the pool prefix {config.name_prefix()!r}")
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
        "project": project, "scope": scope, "name": name.strip(),
        "prompt": prompt or None, "model": model, "effort": effort,
        "permission": permission, "mode": mode,
        "disallowed_tools": _tool_list(args.get("disallowed_tools")),
        "remote_control": _remote_control(args.get("remote_control"), name.strip()),
    }


def _tool_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    items = value.split(",") if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(t, str) for t in items):
        raise Refused("disallowed_tools must be a list of tool names")
    return [t.strip() for t in items if t.strip()]


def _remote_control(value: Any, name: str) -> str | None:
    """The Remote Control name to use, or None when it is off."""
    if value is None or value is False:
        return None
    if value is True:
        return name
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise Refused("remote_control must be true, false or a name")


def build_flags(spec: dict[str, Any]) -> list[str]:
    """The claude flags for a start. `--model` goes last, just before the prompt.

    The model is a flag, not `ANTHROPIC_MODEL`: the flag wins where the two
    disagree, and it is the only per-session setting that is stated in the
    session's own launch record.
    """
    flags = (["--permission-mode", spec["permission"]] if spec["permission"]
             else config.skip_permission_flags())
    if spec["disallowed_tools"]:
        flags += ["--disallowedTools", ",".join(spec["disallowed_tools"])]
    if spec["remote_control"]:
        flags += ["--remote-control", spec["remote_control"]]
    return [*flags, "--effort", spec["effort"], "--model", spec["model"]]


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
    return any(rec.get("name") == name for rec in _interactive_records())


# --- launching ---------------------------------------------------------------


def launch_argv(cwd: Path, name: str, flags: list[str],
                prompt: str | None) -> tuple[list[str], dict[str, str]]:
    """The command that starts one session, and the env it needs.

    `--working-directory` is what sets the session's directory: a user unit
    starts in the home directory whatever the launcher's own cwd was.
    `KillMode=process` keeps a session out of the unit's cgroup teardown.
    The prompt follows a `--`; without it the CLI drops the prompt silently.
    """
    claude = [config.claude_bin(), "--bg", "-n", name, *flags]
    if prompt:
        claude += ["--", prompt]
    bus = _user_manager_env()
    if not bus:
        return claude, {}
    unit = f"awm-cx-start-{uuid.uuid4().hex[:8]}"
    return [
        "systemd-run", "--user", "--quiet", "--collect", f"--unit={unit}",
        "--property=Restart=no", "--property=KillMode=process", "--nice=19",
        f"--working-directory={cwd}", "--", *claude,
    ], bus


def _user_manager_env() -> dict[str, str] | None:
    if not shutil.which("systemd-run"):
        return None
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    if not Path(runtime, "bus").is_socket():
        return None
    return {"XDG_RUNTIME_DIR": runtime,
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus"}


async def launch_session(*, cwd: Path, name: str, flags: list[str],
                         prompt: str | None) -> sessions.Session:
    """Start the session and return the roster's record of it.

    The exit status of the launch is not trusted: the session is the daemon's
    child, so the only proof one exists is a new roster entry carrying the name.
    """
    before = {s.short for s in sessions.load()}
    argv, extra_env = launch_argv(cwd, name, flags, prompt)
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd),
        env={**os.environ, **extra_env, "HOME": str(Path.home())},
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(proc.wait(), timeout=LAUNCH_TIMEOUT_S)
    except (TimeoutError, asyncio.TimeoutError):
        proc.kill()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + LAUNCH_TIMEOUT_S
    while loop.time() < deadline:
        for s in sessions.load():
            if s.short not in before and name in (s.seed_name, s.name):
                return s
        await asyncio.sleep(POLL_S)
    raise TimeoutError(f"no session named {name!r} appeared within "
                       f"{LAUNCH_TIMEOUT_S:.0f}s")


# --- lineage -----------------------------------------------------------------


def _lineage_path(job: str) -> Path:
    return config.starts_dir() / f"{job}.json"


def read_lineage(job: str) -> dict[str, Any] | None:
    if not _JOB.match(job or ""):
        return None
    try:
        data = json.loads(_lineage_path(job).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_lineage(job: str, record: dict[str, Any]) -> None:
    path = _lineage_path(job)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=1))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


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


def prune_lineage(now: float | None = None) -> list[str]:
    """Delete lineage records whose job the roster no longer holds.

    Does nothing when no daemon runs: the roster then reads as empty and every
    record would look orphaned.
    """
    if sessions.daemon_pid() is None:
        return []
    held = {s.short for s in sessions.load()}
    now = time.time() if now is None else now
    gone = []
    for job in _lineage_records():
        path = _lineage_path(job)
        if job in held:
            continue
        try:
            if now - path.stat().st_mtime < PRUNE_GRACE_S:
                continue
            path.unlink()
        except OSError:
            continue
        gone.append(job)
    return gone


def mode_of(caller_pid: int) -> str | None:
    """The mode of the session whose REPL is `caller_pid`, or None.

    None means the pid is no session cx started: an interactive terminal, a
    pool session, a hook, or a pid with no roster entry.
    """
    if not isinstance(caller_pid, int) or isinstance(caller_pid, bool) or caller_pid <= 0:
        return None
    records = _lineage_records()
    if not records:
        return None
    for s in sessions.load():
        if s.repl_pid == caller_pid and sessions.is_alive(s):
            rec = records.get(s.short)
            return rec.get("mode") if rec else None
    return None


def _parent_of(pid: Any) -> str | None:
    """The calling session: its job id when a background job, else its session id."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    for s in sessions.load():
        if s.repl_pid == pid:
            return s.short
    rec = _read_json(config.sessions_dir() / f"{pid}.json")
    return str(rec.get("jobId") or rec.get("sessionId") or f"pid:{pid}")


# --- stop --------------------------------------------------------------------


async def stop(args: dict[str, Any], as_: str | None = None) -> dict[str, Any]:
    """Stop a session `start` created. Its conversation is kept."""
    reason = caller_refusal(as_)
    job = args.get("job")
    if not reason and (not isinstance(job, str) or not _JOB.match(job)):
        reason = "job must be a job id"
    if not reason and read_lineage(job) is None:
        reason = f"job {job} was not started by cx start, so cx will not stop it"
    if reason:
        log.info("cx: stop refused — %s", reason)
        return {"ok": False, "reason": reason}
    ok, detail = await run_claude_stop(job)
    if not ok:
        return {"ok": False, "job": job, "reason": detail}
    return {"ok": True, "job": job}


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
        if sessions._proc_start(pid) != str(rec.get("procStart")):
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
