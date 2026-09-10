"""What the pool knows about a background Claude Code session.

Pure reads. Nothing here starts, moves or deletes anything, which is why the
predicates that decide whether a session may be handed out or thrown away live
in this module and are tested on their own.

Two files describe a session and they do not agree, so which one is asked
matters. The **roster** (`~/.claude/daemon/roster.json`) is the daemon's own
table: it carries liveness, the PTY lane, the CLI version and the start time.
Its `dispatch.seed.name` records what a session was called when it was created
and never changes afterwards. The **state record**
(`~/.claude/jobs/<short>/state.json`) is the session's own, and it is the only
thing that follows a rename.

Identity therefore comes only from the state record. On this box a session
whose roster entry still reads `<spare zorilla>` had been renamed to "remote
shell" and talked to for 27k tokens; reading the name from the roster would
have handed that conversation to the next terminal that ran `cx`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from awm.cx import config


@dataclass(frozen=True)
class Session:
    """One background session, as the roster and its own record describe it."""

    short: str
    # --- from the session's own state record (identity) ---
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
    session_id: str | None
    source: str | None
    seed_name: str | None


# --- the five questions -----------------------------------------------------


def is_alive(s: Session) -> bool:
    """Is the process the roster recorded still the one running under that pid?

    The start time is what pins the identity. Without it a recycled pid passes
    for a session that is gone, and the pool hands out a dead id.
    """
    if not s.repl_proc_start:
        return False
    return _proc_start(s.repl_pid) == s.repl_proc_start


def is_ours(s: Session) -> bool:
    """Does this session's own record still carry the pool's name prefix?

    A rename takes a session out of the pool by this alone, which is the single
    cheapest protection here: the first thing a user or an agent does with a
    session it has adopted is give it a name.
    """
    return s.name.startswith(config.name_prefix())


def is_untouched(s: Session) -> bool:
    """Has nobody prompted this session or taken it anywhere?

    Five witnesses, of which two are load-bearing. `tokens` and `origin_cwd`
    survive a stop, a retire and a respawn. `needs`, `intent` and
    `first_terminal_at` are cleared or stamped by a stop, so on a session that
    has been renamed and used they can read exactly as pristine — they join the
    test as an AND, they do not carry it.

    `origin_cwd` appears the first time a session is moved. A session that has
    been moved already carries the CLAUDE.md of the directory it was taken to,
    and moving it again would carry that file into somebody else's project.
    """
    return (
        s.tokens == 0
        and s.origin_cwd is None
        and (s.intent or "") == ""
        and s.needs == "send a prompt to start"
        and s.first_terminal_at is None
    )


def claimable(s: Session, *, version: str | None, now: float | None = None) -> bool:
    """May this session be handed to a terminal?

    Fails closed when the binary's version cannot be resolved: a session seeded
    by a binary that is no longer installed will not attach.
    """
    if version is None:
        return False
    return (
        is_ours(s)
        and is_untouched(s)
        and is_alive(s)
        and s.cli_version == version
        and age_s(s, now) < config.rotate_age_s()
    )


def removable(s: Session, *, version: str | None, now: float | None = None) -> bool:
    """May this session be deleted?

    Deliberately not the negation of `claimable`. "Delete whatever cannot be
    claimed" would delete the session somebody is attached to and typing into.

    Removable means the pool made it (`is_ours`), nobody spoke to it (no
    tokens), and either the process is gone, or it has never been moved and is
    stale by version or by age. A *live* session that has been moved belongs to
    whoever took it: leave it to the daemon's own retirement and collect the
    corpse afterwards.

    Fails open on an unresolvable version, in the sense that the version clause
    is simply dropped. Failing closed on it the other way would make every
    session look stale the moment the binary's symlink changed shape, and the
    pool would delete itself on a loop.
    """
    if not is_ours(s) or s.tokens != 0 or (s.intent or "") != "":
        return False
    if not is_alive(s):
        return True
    if s.origin_cwd is not None:
        return False
    stale_version = version is not None and s.cli_version != version
    return stale_version or age_s(s, now) >= config.rotate_age_s()


def age_s(s: Session, now: float | None = None) -> float:
    """Seconds since the daemon started this session, or 0 if it never said."""
    if not s.started_at_ms:
        return 0.0
    return max(0.0, (now if now is not None else time.time()) - s.started_at_ms / 1000.0)


# --- loading ----------------------------------------------------------------


def binary_version() -> str | None:
    """The version directory the `claude` on PATH resolves to, e.g. "2.1.268".

    Read off the symlink rather than by running the binary: this is on the path
    of every tick and every claim, and `claude --version` costs a process.
    """
    try:
        target = Path(config.claude_bin()).resolve(strict=True)
    except OSError:
        return None
    name = target.name
    return name or None


def daemon_pid() -> int | None:
    """The pid of the running Claude Code daemon, or None if there is none.

    This is the precondition on seeding. Whichever process first launches a
    background session after a reboot donates its control group *and its
    environment* to every background session on the node, because they are all
    children of that first daemon. Seeding from inside the gateway when no
    daemon exists would therefore put every one of the user's sessions inside
    `awm.service` — where the next deploy kills them — and hand them an
    environment with no `~/.local/bin` on its PATH.
    """
    roster = _read_json(config.roster_path())
    pid = roster.get("supervisorPid") if isinstance(roster, dict) else None
    if not isinstance(pid, int):
        return None
    return pid if _proc_start(pid) is not None else None


def load() -> list[Session]:
    """Every background session the daemon knows about, newest last."""
    roster = _read_json(config.roster_path())
    workers = roster.get("workers") if isinstance(roster, dict) else None
    if not isinstance(workers, dict):
        return []
    out = [_build(short, w) for short, w in workers.items() if isinstance(w, dict)]
    out.sort(key=lambda s: s.started_at_ms or 0)
    return out


def _build(short: str, w: dict) -> Session:
    st = _read_json(config.jobs_dir() / short / "state.json")
    dispatch = w.get("dispatch") if isinstance(w.get("dispatch"), dict) else {}
    seed = dispatch.get("seed") if isinstance(dispatch.get("seed"), dict) else {}
    return Session(
        short=short,
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
        session_id=_as_str(w.get("sessionId")),
        source=_as_str(dispatch.get("source")),
        seed_name=_as_str(seed.get("name")),
    )


def _as_str(v: object) -> str | None:
    return None if v is None else str(v)


def _read_json(path: Path) -> dict:
    try:
        with open(path, "rb") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _proc_start(pid: int | None) -> str | None:
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
