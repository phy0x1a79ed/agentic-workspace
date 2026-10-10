"""Reading what Claude Code's daemon and its sessions have written to disk.

Pure reads. Nothing here starts, moves or deletes a session. Three records
describe a background session and they do not agree, so which one is asked
matters:

* the **roster** (`~/.claude/daemon/roster.json`) is the daemon's own table:
  liveness, the PTY lane, the CLI version, the start time, and the name the
  session was created under (`dispatch.seed.name`, which never changes);
* the **job record** (`~/.claude/jobs/<short>/state.json`) is the session's own,
  and the only one that follows a rename;
* the **session record** (`~/.claude/sessions/<repl-pid>.json`) is written per
  running REPL, interactive or background, and says how the process is hosted.

Every path is a parameter that defaults to the live home, so a caller aimed at a
test directory or a shadow run says so at the call. Deciding what a record means
for a caller (is this session ours, may it be reached) stays with the caller.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


class SessionRecordError(RuntimeError):
    """A session record is missing, unreadable, or describes another process."""


class Unreadable(RuntimeError):
    """A record exists but could not be read, so what it holds is unknown.

    Distinct from a record that is absent, which reads as empty: a caller that
    must not act on a guess (a sweep deciding what is live) treats this as
    "assume live" and never as "nothing there".

    ``path`` is the exact file (or directory) that could not be read, so an
    operator can repair or delete it.
    """

    def __init__(self, path: Path, why: str):
        self.path = Path(path)
        super().__init__(f"cannot read {path} ({why}); what it holds is unknown")


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def default_roster_path() -> Path:
    return claude_home() / "daemon" / "roster.json"


def default_jobs_dir() -> Path:
    return claude_home() / "jobs"


def default_sessions_dir() -> Path:
    return claude_home() / "sessions"


@dataclass(frozen=True)
class Session:
    """One background session, as the roster and its own record describe it."""

    short: str
    # --- from the session's own state record (identity) ---
    #: False when the record is absent, which is what `claude rm` leaves
    #: behind for the moment before the daemon clears the roster entry.
    has_record: bool
    name: str
    tokens: int
    intent: str
    needs: str | None
    origin_cwd: str | None
    cwd: str | None
    first_terminal_at: str | None
    # --- from the daemon's roster (liveness and transport) ---
    repl_pid: int | None
    repl_proc_start: str | None
    started_at_ms: int | None
    cli_version: str | None
    pty_sock: str | None
    pty_auth: str | None
    dec_modes: tuple[int, ...]
    session_id: str | None
    source: str | None
    seed_name: str | None


# --- the process table ------------------------------------------------------


def proc_start(pid: int | None) -> str | None:
    """Field 22 of /proc/<pid>/stat — the process's start time in clock ticks.

    The comm field can contain spaces and parentheses, so the split is on the
    last `) ` rather than on whitespace.
    """
    if not pid:
        return None
    try:
        with open(f"/proc/{pid}/stat", "r") as fh:
            raw = fh.read()
    except OSError:
        return None
    _, _, rest = raw.partition(") ")
    fields = rest.split()
    # stat field 22 is the 20th after the comm field's closing paren.
    return fields[19] if len(fields) > 19 else None


def ppid_children() -> dict[int, list[int]]:
    """Map ppid → [child pids] by scanning /proc (Linux)."""
    kids: dict[int, list[int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return kids
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8") as fp:
                data = fp.read()
            # comm (field 2) is parenthesized and may contain spaces; ppid is
            # the field right after the closing paren.
            fields = data[data.rfind(")") + 2:].split()
            ppid = int(fields[1])
        except (OSError, ValueError, IndexError):
            continue
        kids.setdefault(ppid, []).append(int(entry))
    return kids


def subtree_contains(root_pid: int, target_pid: int,
                     kids: dict[int, list[int]]) -> bool:
    """Is ``target_pid`` ``root_pid`` or anywhere beneath it in ``kids``?"""
    stack, seen = [root_pid], set()
    while stack:
        pid = stack.pop()
        if pid == target_pid:
            return True
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(kids.get(pid, []))
    return False


def attached_shorts() -> frozenset[str]:
    """The sessions a terminal is looking at, read off the process table.

    No file records this. The daemon multiplexes every attach over its one
    control socket, so a session's own PTY and rendezvous sockets carry exactly
    the same two connections whether a terminal is on them or not, and nothing
    is written to the state record either. What is left is the attaching
    process: `cx` reaches a session by running `claude attach <short>`, so the
    short id sits in an argv for as long as that terminal is open.

    CAUTION: a terminal that arrived some other way is invisible here, and an
    unreadable /proc makes every session look unattached. This is the last
    guard before a deletion and never the only one — age, tokens and intent all
    have to agree first.
    """
    out: set[str] = set()
    try:
        pids = os.listdir("/proc")
    except OSError:
        return frozenset()
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = fh.read().split(b"\0")
        except OSError:
            continue
        try:
            short = argv[argv.index(b"attach") + 1]
        except (ValueError, IndexError):
            continue
        if short:
            out.add(short.decode(errors="replace"))
    return frozenset(out)


# --- json -------------------------------------------------------------------


def read_json(path: Path) -> dict:
    """The object in ``path``, or ``{}`` for anything else — absent, unreadable
    or not an object. Use `read_json_strict` when "cannot tell" matters."""
    try:
        with open(path, "rb") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_json_strict(path: Path) -> dict:
    """The object in ``path``; ``{}`` only when the file is absent.

    Raises `Unreadable` when the file exists but cannot be read or parsed, or
    does not hold an object.
    """
    try:
        with open(path, "rb") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise Unreadable(path, str(exc)) from None
    if not isinstance(data, dict):
        raise Unreadable(path, "not a JSON object")
    return data


# --- the roster and job records ---------------------------------------------


def daemon_pid(roster_path: Path | None = None) -> int | None:
    """The pid of the running Claude Code daemon, or None if there is none.

    This is the precondition on launching. Whichever process first launches a
    background session after a reboot donates its control group *and its
    environment* to every background session on the node, because they are all
    children of that first daemon. Launching from inside the gateway when no
    daemon exists would therefore put every one of the user's sessions inside
    `awm.service` — where the next deploy kills them — and hand them an
    environment with no `~/.local/bin` on its PATH.
    """
    roster = read_json(roster_path or default_roster_path())
    pid = roster.get("supervisorPid")
    if not isinstance(pid, int):
        return None
    return pid if proc_start(pid) is not None else None


def load(roster_path: Path | None = None,
         jobs_dir: Path | None = None) -> list[Session]:
    """Every background session the daemon knows about, newest last."""
    roster = read_json(roster_path or default_roster_path())
    workers = roster.get("workers")
    if not isinstance(workers, dict):
        return []
    jobs = jobs_dir or default_jobs_dir()
    out = [build_session(short, w, jobs) for short, w in workers.items()
           if isinstance(w, dict)]
    out.sort(key=lambda s: s.started_at_ms or 0)
    return out


def build_session(short: str, w: dict, jobs_dir: Path | None = None) -> Session:
    st = read_json((jobs_dir or default_jobs_dir()) / short / "state.json")
    dispatch = w.get("dispatch") if isinstance(w.get("dispatch"), dict) else {}
    seed = dispatch.get("seed") if isinstance(dispatch.get("seed"), dict) else {}
    return Session(
        short=short,
        has_record=bool(st),
        name=str(st.get("name") or ""),
        tokens=int(st.get("tokens") or 0),
        intent=str(st.get("intent") or ""),
        needs=st.get("needs"),
        origin_cwd=st.get("originCwd"),
        cwd=st.get("cwd"),
        first_terminal_at=st.get("firstTerminalAt"),
        repl_pid=w.get("replPid"),
        repl_proc_start=_as_str(w.get("replProcStart")),
        started_at_ms=w.get("startedAt"),
        cli_version=_as_str(w.get("cliVersion")),
        pty_sock=_as_str(w.get("ptySock")),
        pty_auth=_as_str(w.get("ptyAuth")),
        dec_modes=tuple(m for m in (w.get("decModes") or [])
                        if isinstance(m, int)),
        session_id=_as_str(w.get("sessionId")),
        source=_as_str(dispatch.get("source")),
        seed_name=_as_str(seed.get("name")),
    )


def _as_str(v: object) -> str | None:
    return None if v is None else str(v)


def binary_version(claude_bin: str | Path) -> str | None:
    """The version directory the `claude` binary resolves to, e.g. "2.1.268".

    Read off the symlink rather than by running the binary: this is on the path
    of every tick and every claim, and `claude --version` costs a process.
    """
    try:
        target = Path(claude_bin).resolve(strict=True)
    except OSError:
        return None
    name = target.name
    return name or None


# --- session records, by REPL pid -------------------------------------------


def read_session_record(repl_pid: int, *, sessions_dir: Path | None = None,
                        proc_start_fn: Callable[[int], Optional[str]] = proc_start
                        ) -> dict:
    """Return Claude Code's session record for ``repl_pid``.

    Raises :class:`SessionRecordError` if there is no record, it is unreadable,
    or it describes a *different* process that has since inherited this pid.
    Records are keyed by pid and pids get recycled, so the record's ``procStart``
    is checked against the live process's start time before it is trusted —
    without that, a long-dead session's record could point a caller at whatever
    now holds its number.
    """
    path = (sessions_dir or default_sessions_dir()) / f"{repl_pid}.json"
    try:
        record = json.loads(path.read_text())
    except FileNotFoundError:
        raise SessionRecordError(
            f"no Claude Code session record for pid {repl_pid}; the caller does "
            f"not look like a Claude Code session, so there is nothing to inject "
            f"into") from None
    except (OSError, ValueError) as exc:
        raise SessionRecordError(
            f"could not read session record for pid {repl_pid}: {exc}") from None

    live = proc_start_fn(repl_pid)
    if live is None:
        raise SessionRecordError(f"calling process {repl_pid} is gone")
    claimed = str(record.get("procStart", ""))
    if claimed and claimed != live:
        raise SessionRecordError(
            f"session record for pid {repl_pid} is stale (it describes a process "
            f"started at {claimed}, but pid {repl_pid} started at {live} — the pid "
            f"was recycled); refusing to inject")
    return record


def session_by_pid(pid: int, *, sessions_dir: Path | None = None) -> dict | None:
    """The live session record for ``pid``, or None when there is none to trust.

    Where `read_session_record` explains why it refused, this answers a yes/no
    question: is this pid a running Claude Code REPL, and what does it say it is
    (``kind``, ``sessionId``, ``jobId``, ``name``). Never raises.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        return read_session_record(pid, sessions_dir=sessions_dir)
    except SessionRecordError:
        return None


def job_of_pid(pid: int, *, sessions_dir: Path | None = None) -> str | None:
    """The background job's short id when ``pid`` is a background REPL."""
    rec = session_by_pid(pid, sessions_dir=sessions_dir)
    if not rec or rec.get("kind") != "bg":
        return None
    return str(rec.get("jobId") or "") or None


# --- sessions that are running right now ------------------------------------


def live_session_ids(*, sessions_dir: Path | None = None,
                     roster_path: Path | None = None,
                     jobs_dir: Path | None = None,
                     running: Callable[[int], bool] | None = None) -> set[str]:
    """Conversation ids that belong to a Claude Code process running right now.

    Three places name one, and all three are read because none is complete on
    its own: the per-pid session records (checked against the process table,
    because the file outlives the process), the daemon roster's ``sessionId``,
    and each job record's ``sessionId`` / ``resumeSessionId``, which is where a
    cleared or respawned conversation went.

    Absent files read as empty. A file that is present and cannot be read raises
    `Unreadable` rather than contributing nothing: the caller is deciding whether
    something may be deleted, and "could not tell" must not look like "none".
    """
    sessions_d = sessions_dir or default_sessions_dir()
    jobs_d = jobs_dir or default_jobs_dir()
    alive = running or (lambda pid: Path(f"/proc/{pid}").exists())
    ids: set[str] = set()

    try:
        records = list(sessions_d.glob("*.json"))
    except OSError as exc:
        raise Unreadable(sessions_d, str(exc)) from None
    for rec in records:
        try:
            pid = int(rec.stem)
        except ValueError:
            continue
        if not alive(pid):
            continue
        sid = read_json_strict(rec).get("sessionId")
        if sid:
            ids.add(str(sid))

    roster = read_json_strict(roster_path or default_roster_path())
    for worker in (roster.get("workers") or {}).values():
        sid = worker.get("sessionId") if isinstance(worker, dict) else None
        if sid:
            ids.add(str(sid))

    try:
        jobs = [d for d in jobs_d.iterdir() if d.is_dir()]
    except FileNotFoundError:
        jobs = []
    except OSError as exc:
        raise Unreadable(jobs_d, str(exc)) from None
    for job in jobs:
        state = read_json_strict(job / "state.json")
        for key in ("sessionId", "resumeSessionId"):
            sid = state.get(key)
            if sid:
                ids.add(str(sid))

    return ids
