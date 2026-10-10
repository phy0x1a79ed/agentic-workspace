"""Which restricted mode, if any, a Claude Code session was started in.

`cx start` declares a mode for each session it creates and writes it to a
lineage record under cx's state directory. The gateway gates a calling session
by that mode, and cx gates what a session may start or stop by it, so both read
the answer from here. Everything is read from files on disk (Claude Code's
per-process session records, the daemon roster, the job records and cx's
lineage records), so the gateway needs no IPC to the cx process.

The paths are resolved from the same environment variables cx uses, in this one
place. A reader that located the lineage directory differently from the writer
would see no record for any session and answer "not restricted".

A caller is only ever held to its own records. The caller's session record is
read first, and anything that is not a background session answers `None` before
the roster or the lineage directory is opened, so a damaged record belonging to
another job cannot lock out an interactive terminal.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from awm.claudedaemon import roster

UNKNOWN = "unknown"

#: A mode is a short label. A recorded mode that fails this is corrupt.
MODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
JOB_PATTERN = re.compile(r"^[0-9a-f]{8}$")

#: How far up the process tree a child looks for the session that started it.
MAX_ANCESTRY_HOPS = 16


# --- where cx keeps its records ---------------------------------------------


def _home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def roster_path() -> Path:
    return Path(os.environ.get("AWM_CX_ROSTER") or (_home() / "daemon" / "roster.json"))


def jobs_dir() -> Path:
    return Path(os.environ.get("AWM_CX_JOBS") or (_home() / "jobs"))


def sessions_dir() -> Path:
    return Path(os.environ.get("AWM_CX_SESSIONS") or (_home() / "sessions"))


def state_dir() -> Path:
    return Path(os.environ.get("AWM_CX_STATE") or (_home() / "cx"))


def starts_dir() -> Path:
    """One JSON file per started session, plus `pending-*` files written before
    a launch."""
    return state_dir() / "starts"


def pending_path(name: str) -> Path:
    """Where the record of a start in flight for the session `name` lives."""
    digest = hashlib.sha256(name.encode()).hexdigest()[:16]
    return starts_dir() / f"pending-{digest}.json"


# --- the mode ---------------------------------------------------------------


class _Unreadable(Exception):
    """A record the answer needs is missing or unreadable."""


def mode_of(caller_pid: int) -> str | None:
    """The mode of the session whose REPL is `caller_pid`.

    Three answers, and a gate must treat them differently:

    - a mode string: `caller_pid` is a session cx started (or is about to adopt),
      and this is the mode it was declared with;
    - `None`: `caller_pid` is positively not a cx-started session. It is an
      interactive terminal, a pool session, or a hand-started job, and its own
      records say so;
    - `"unknown"`: anything else, including an invalid pid, one of the caller's
      own records being unreadable or stale, a background session with no
      record of how it was started, and a background job that carries cx's
      launch flags but no lineage. Treat it as the most restricted mode.
    """
    if not isinstance(caller_pid, int) or isinstance(caller_pid, bool) or caller_pid <= 0:
        return UNKNOWN
    try:
        return _mode_of(caller_pid)
    except (_Unreadable, roster.Unreadable, OSError, ValueError, TypeError):
        return UNKNOWN


class _Roster:
    """The roster's workers, read at most once for one `mode_of` call.

    A failed read is remembered too, so a damaged roster costs one read, not
    one per ancestor.
    """

    def __init__(self) -> None:
        self._workers: dict[str, Any] | None = None
        self._error: Exception | None = None

    def workers(self) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        if self._workers is None:
            try:
                self._workers = _workers(strict=True) or {}
            except (_Unreadable, roster.Unreadable, OSError, ValueError, TypeError) as exc:
                self._error = exc
                raise
        return self._workers


def _mode_of(pid: int) -> str | None:
    if roster.proc_start(pid) is None:
        # No such process: nothing about it can be positively known, and the
        # ancestor walk below would read a missing /proc entry as "no parent".
        raise _Unreadable(f"pid {pid} is not a running process")
    view = _Roster()
    rec, claims_job = _verified_record(pid)
    if rec is not None and rec.get("kind") == "bg":
        return _bg_mode(rec)
    parked = _parked_mode(rec)
    if parked is not None:
        return parked
    if rec is None:
        listed, unrecorded = _unrecorded_mode(pid, view)
        if listed:
            # The roster names this process as a job REPL, and says whether cx
            # started it. Nothing above it can change that.
            return unrecorded
    inherited = _inherited_mode(pid, view)
    if inherited is None and claims_job:
        return UNKNOWN
    return inherited


def _verified_record(pid: int) -> tuple[dict[str, Any] | None, bool]:
    """The session record of `pid`, and whether an untrusted one claimed a job.

    A record that carries no `procStart` cannot be told from one left behind by
    an earlier process with the same pid, so it is not allowed to grant anything,
    and above all not `None`. It is dropped, and the roster and the ancestors
    decide. Only when they find nothing, and the dropped record said it was a
    background session or a parked job, does the caller get `unknown`: a record
    that says "I am a job" must not turn into "I am an ordinary terminal".
    """
    rec = _session_record(pid)
    if rec is None or rec.get("procStart"):
        return rec, False
    return None, rec.get("kind") == "bg" or isinstance(rec.get("parkedJobId"), str)


def _bg_mode(rec: dict[str, Any]) -> str | None:
    job = rec.get("jobId")
    if not isinstance(job, str) or not JOB_PATTERN.match(job):
        raise _Unreadable("a background session record carries no job id")
    worker = (_workers(strict=False) or {}).get(job)
    declared = _declared(job, _names(job, worker, rec.get("name")))
    if declared is not None:
        return declared
    worker = (_workers(strict=True) or {}).get(job)
    if not isinstance(worker, dict):
        raise _Unreadable("the roster does not list the background session")
    return UNKNOWN if _looks_cx_started(worker) else None


def _parked_mode(rec: dict[str, Any] | None) -> str | None:
    """The mode of the job an interactive session took over.

    Attaching a terminal to a background job parks the job and runs its
    conversation in an interactive process, so the process that calls is not the
    job's REPL. It keeps the job's mode, or attaching would lift the gate.
    """
    parked = (rec or {}).get("parkedJobId")
    if not isinstance(parked, str) or not JOB_PATTERN.match(parked):
        return None
    worker = (_workers(strict=False) or {}).get(parked)
    return _declared(parked, _names(parked, worker, (rec or {}).get("name")))


def _inherited_mode(pid: int, view: _Roster) -> str | None:
    """The mode of the nearest ancestor process that is a cx-started session.

    A process a restricted session starts (a `claude -p` child, a teammate, an
    MCP proxy behind a wrapper) is not itself the session, and without this walk
    it would be ungated. The first ancestor with a mode, an `unknown` included,
    decides. Every ancestor is looked up in the roster, because a background
    REPL may have no session record or a stale one: a record that is missing,
    stale or unreadable counts as no record. A background record that is
    readable is read as the session's own.
    """
    current = pid
    for _ in range(MAX_ANCESTRY_HOPS):
        parent = _ppid(current)
        if parent is None:
            # The process was alive a moment ago, so an unreadable parent is a
            # process that died under us, not the top of the tree. A reparented
            # process shows ppid 1, which ends the walk below.
            return UNKNOWN
        current = parent
        if current <= 1:
            return None
        try:
            rec, claims_job = _verified_record(current)
        except (_Unreadable, OSError, ValueError, TypeError):
            rec, claims_job = None, False
        try:
            if rec is None:
                mode = _unrecorded_mode(current, view)[1]
                if mode is None and claims_job:
                    return UNKNOWN
            else:
                mode = _bg_mode(rec) if rec.get("kind") == "bg" else _parked_mode(rec)
        except (_Unreadable, roster.Unreadable, OSError, ValueError, TypeError):
            return UNKNOWN
        if mode is not None:
            return mode
    return None


_NODE_LIKE = frozenset({"node", "nodejs", "bun"})
_CLAUDE_DIRS = frozenset({"claude", "claude-code"})


def _path_parts(path: str) -> list[str]:
    return [p for p in path.split("/") if p]


def _looks_like_claude(argv: list[str], exe: str | None) -> bool:
    """Whether a command line or an executable path is Claude Code's.

    Judged on argv[0] (and argv[1] behind a JavaScript runtime) and on the
    executable, never on the arguments: a path argument such as `claudedaemon`
    or `~/.claude` says nothing about the process.
    """
    def claude_path(path: str) -> bool:
        parts = _path_parts(path)
        return bool(parts) and (parts[-1].startswith("claude") and parts[-1] != "claudedaemon"
                                or any(p in _CLAUDE_DIRS for p in parts))

    if exe and claude_path(exe):
        return True
    if not argv:
        return False
    if claude_path(argv[0]):
        return True
    runtime = os.path.basename(argv[0])
    return runtime in _NODE_LIKE and len(argv) > 1 and claude_path(argv[1])


def _may_be_a_session(pid: int) -> bool:
    """Whether a process could be a Claude Code REPL, for judging a damaged roster.

    This decides only how a roster that cannot be read is treated: `unknown` for
    a process that may be a session, "no information" for a shell or a wrapper.
    A readable roster is always consulted. The command line and the executable
    are read separately and either one that answers decides, because the
    executable link is refused for another user's and non-dumpable processes
    (sshd, sudo, login) while the command line is not. Only when neither can be
    read is the process assumed to be a session.
    """
    argv: list[str] | None = None
    exe: str | None = None
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            argv = [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
    except OSError:
        pass
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        pass
    if not argv and exe is None:
        return True
    return _looks_like_claude(argv or [], exe)


def _ppid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return int(fh.read().rpartition(") ")[2].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def _unrecorded_mode(pid: int, view: _Roster) -> tuple[bool, str | None]:
    """A pid with no session record: the roster is the only thing that can name it.

    Returns whether the roster lists the process as a job REPL, and the mode it
    gives it (None for a job cx did not start). A roster that cannot be read says
    nothing about a shell, so it is an error only for a process that may be a
    Claude Code session.
    """
    try:
        workers = view.workers()
    except (_Unreadable, roster.Unreadable, OSError, ValueError, TypeError):
        if _may_be_a_session(pid):
            raise
        return False, None
    for job, worker in workers.items():
        if (isinstance(worker, dict) and worker.get("replPid") == pid
                and roster.proc_start(pid) == str(worker.get("replProcStart"))):
            declared = _declared(job, _names(job, worker, None))
            if declared is not None:
                return True, declared
            return True, UNKNOWN if _looks_cx_started(worker) else None
    return False, None


def _workers(*, strict: bool) -> dict[str, Any] | None:
    """The roster's workers. Absent means none; unreadable raises only when strict."""
    data = roster.read_json_strict(roster_path()) if strict else roster.read_json(roster_path())
    workers = data.get("workers")
    if workers is None:
        return {}
    if not isinstance(workers, dict):
        if strict:
            raise _Unreadable("the roster's workers are not an object")
        return {}
    return workers


def _names(job: str, worker: Any, session_name: Any) -> set[str]:
    """Every name a pending start for this job could have been filed under."""
    found: set[Any] = {session_name}
    if isinstance(worker, dict):
        found.add(((worker.get("dispatch") or {}).get("seed") or {}).get("name"))
    found.add(roster.read_json(jobs_dir() / job / "state.json").get("name"))
    return {n for n in found if isinstance(n, str) and n}


def _declared(job: str, names: set[str]) -> str | None:
    """The mode recorded for this job, else for a pending start of its name.

    Only the files this job could own are read: its lineage file, and the
    pending file of each of its names. A damaged record for any other job is
    not this caller's business.
    """
    record = _read_record(starts_dir() / f"{job}.json")
    if record is None:
        for name in sorted(names):
            record = _read_record(pending_path(name))
            if record is not None:
                break
    if record is None:
        return None
    mode = record.get("mode")
    if not isinstance(mode, str) or not MODE_PATTERN.match(mode):
        raise _Unreadable(f"the record of {job} carries no valid mode")
    return mode


def _read_record(path: Path) -> dict[str, Any] | None:
    """The object in `path`; None when the file is absent; raises when damaged."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    if not isinstance(data, dict):
        raise _Unreadable(f"{path.name} is not an object")
    return data


def _looks_cx_started(worker: dict[str, Any]) -> bool:
    """Whether the job's recorded launch flags are ones only cx writes.

    This is a fallback for a job whose lineage record is gone, so it matches the
    exact `=` forms `build_flags` writes and nothing a person types: a person
    passes `--permission-mode plan` as two tokens, and a resumed job keeps them
    that way. A start with `--permission-mode=<mode>` or `--restricted` is cx's.
    So is a start that passes both `--effort=` and `--model=`, which covers a
    skip-permissions start. A resumed job keeps its flags under
    `dispatch.launch.flagArgs`. The roster records no environment variable cx
    could set as a stronger marker: only provider variables survive the launch.
    """
    dispatch = worker.get("dispatch") if isinstance(worker.get("dispatch"), dict) else {}
    launch = dispatch.get("launch") if isinstance(dispatch.get("launch"), dict) else {}
    tokens: list[Any] = []
    for source in (launch.get("args"), launch.get("flagArgs"),
                   dispatch.get("respawnFlags"), worker.get("respawnFlags")):
        if isinstance(source, list):
            tokens.extend(source)
    strings = [t for t in tokens if isinstance(t, str)]
    equals = {t.partition("=")[0] for t in strings if "=" in t}
    return ("--restricted" in strings or "--permission-mode" in equals
            or {"--effort", "--model"} <= equals)


def _session_record(pid: int) -> dict[str, Any] | None:
    """Claude Code's record of the live process `pid`; None when it has none.

    A missing sessions directory means no session has run here, so the caller
    has no record. A record that is unreadable, or that describes another
    process, is the caller's own damaged record and raises.
    """
    path = sessions_dir() / f"{pid}.json"
    data = _read_record(path)
    if data is None:
        return None
    live = roster.proc_start(pid)
    claimed = str(data.get("procStart", ""))
    if live is None or (claimed and claimed != live):
        raise _Unreadable(f"the record of pid {pid} is stale")
    return data
