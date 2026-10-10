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

#: Launch flags that only `cx start` passes on its own sessions. A background
#: job carrying one but no lineage record was started by cx and lost its record.
_CX_LAUNCH_FLAGS = ("--permission-mode", "--restricted")


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


def _mode_of(pid: int) -> str | None:
    rec = _session_record(pid)
    if rec is not None:
        if rec.get("kind") != "bg":
            return None
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
    return _unrecorded_mode(pid)


def _unrecorded_mode(pid: int) -> str | None:
    """A pid with no session record: the roster is the only thing that can name it."""
    for job, worker in (_workers(strict=True) or {}).items():
        if (isinstance(worker, dict) and worker.get("replPid") == pid
                and roster.proc_start(pid) == str(worker.get("replProcStart"))):
            declared = _declared(job, _names(job, worker, None))
            if declared is not None:
                return declared
            return UNKNOWN if _looks_cx_started(worker) else None
    return None


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
    """Whether the job's recorded launch arguments are ones only cx passes."""
    dispatch = worker.get("dispatch") if isinstance(worker.get("dispatch"), dict) else {}
    launch = dispatch.get("launch") if isinstance(dispatch.get("launch"), dict) else {}
    tokens: list[Any] = []
    for source in (launch.get("args"), worker.get("respawnFlags")):
        if isinstance(source, list):
            tokens.extend(source)
    return any(isinstance(t, str) and t.partition("=")[0] in _CX_LAUNCH_FLAGS for t in tokens)


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
