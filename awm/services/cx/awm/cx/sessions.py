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

They answer different questions and both are asked. *Who does this session
belong to now* is the state record's, because only it follows a rename: on this
box a session whose roster entry still read `<spare zorilla>` had been renamed
to "remote shell" and talked to for 27k tokens, and reading the name from the
roster would have handed that conversation to the next terminal that ran `cx`.
*Did the pool make this session* is the roster's, because the pool renames a
session itself the moment it hands one out, and a predicate reading the current
name would lose sight of everything it ever gave away.

So `is_ours` reads the state record and decides who may be handed a session,
and `was_ours` reads the roster and decides what the pool may collect. Confusing
the two is how a claimed session either gets handed out twice or leaks forever.
"""

from __future__ import annotations

import time

from awm.claudedaemon import roster
# Re-exported: cx's other modules and tests reach these as `sessions.<name>`.
from awm.claudedaemon.roster import Session, attached_shorts  # noqa: F401
from awm.cx import config

_proc_start = roster.proc_start
_read_json = roster.read_json


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


def was_ours(s: Session) -> bool:
    """Did the pool create this session, whatever it is called now?

    The roster stamps `dispatch.seed.name` at launch and never changes it, so
    it still answers after a rename — including the rename the pool performs on
    itself when it hands a session out. `is_ours` is the narrower question and
    stays the claimability test: a session carrying any other name must never
    be handed to a second terminal.
    """
    return (s.seed_name or "").startswith(config.name_prefix())


def is_untouched(s: Session) -> bool:
    """Has nobody prompted this session or taken it anywhere?

    Five witnesses, of which two are load-bearing. `tokens` and `origin_cwd`
    survive a stop, a retire and a respawn. `needs` and `intent` are cleared or
    rewritten by a stop, so on a session that has been renamed and used they can
    read exactly as pristine — they join the test as an AND, they do not carry
    it.

    CAUTION: `first_terminal_at` is not a witness of a terminal. It is stamped
    when the job first reaches a *terminal state*, done or failed, and it stays
    null for the whole life of a session nobody ever prompts. It is here because
    a stamped one proves the session ran something, not because an unstamped one
    proves nobody is looking at it. Attachment has its own answer in
    `attached_shorts`, and it is not in this file's gift.

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


def removable(s: Session, *, version: str | None, now: float | None = None,
              attached: frozenset[str] | None = None) -> bool:
    """May this session be deleted?

    Deliberately not the negation of `claimable`. "Delete whatever cannot be
    claimed" would delete the session somebody is attached to and typing into.

    Keyed on `was_ours`, not `is_ours`: the pool renames a session itself when
    it hands one out, and a predicate that reads the current name would lose
    sight of everything it ever gave away. Nothing else widens with it. A
    session carrying tokens, an intent, or a terminal is refused here whoever
    named it.

    Three ways to qualify. The process is gone and the record is a corpse. The
    session was never moved and is stale by version or by age, which is
    ordinary rotation. Or it was claimed, never spoken to, and has outlived the
    rotate age — the terminal that took it has gone and left it to idle until
    the daemon retires it an hour later.

    Fails open on an unresolvable version, in the sense that the version clause
    is simply dropped. Failing closed on it the other way would make every
    session look stale the moment the binary's symlink changed shape, and the
    pool would delete itself on a loop.
    """
    if not was_ours(s) or s.tokens != 0 or (s.intent or "") != "":
        return False
    if not is_alive(s):
        return True
    if s.short in (attached_shorts() if attached is None else attached):
        return False
    if age_s(s, now) >= config.rotate_age_s():
        return True
    stale_version = version is not None and s.cli_version != version
    return s.origin_cwd is None and stale_version


def age_s(s: Session, now: float | None = None) -> float:
    """Seconds since the daemon started this session, or 0 if it never said."""
    if not s.started_at_ms:
        return 0.0
    return max(0.0, (now if now is not None else time.time()) - s.started_at_ms / 1000.0)


# --- loading ----------------------------------------------------------------


#
# The reads live in `awm.claudedaemon.roster`. These bind them to the paths cx is
# configured with, so an environment override reaches them.


def binary_version() -> str | None:
    """The version directory the `claude` on PATH resolves to, e.g. "2.1.268"."""
    return roster.binary_version(config.claude_bin())


def daemon_pid() -> int | None:
    """The pid of the running Claude Code daemon, or None if there is none."""
    return roster.daemon_pid(config.roster_path())


def load() -> list[Session]:
    """Every background session the daemon knows about, newest last."""
    return roster.load(config.roster_path(), config.jobs_dir())


def _build(short: str, w: dict) -> Session:
    return roster.build_session(short, w, config.jobs_dir())
