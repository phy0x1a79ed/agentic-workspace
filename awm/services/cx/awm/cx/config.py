"""Where the pool reads and writes, and the knobs that shape it.

Every value is overridable from the environment so a shadow run can be aimed
somewhere harmless, and so the tests never touch the real home directory.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def _home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


#: The daemon's roster of background sessions. Liveness, the PTY lane, the CLI
#: version and the start time come from here; nothing about identity does.
def roster_path() -> Path:
    return Path(os.environ.get("AWM_CX_ROSTER") or (_home() / "daemon" / "roster.json"))


#: One directory per session, holding the session's own state record. This is
#: the only source of identity: whether a session has been renamed, prompted or
#: moved.
def jobs_dir() -> Path:
    return Path(os.environ.get("AWM_CX_JOBS") or (_home() / "jobs"))


#: Sessions are named so they read as tooling in `claude agents` and nobody
#: deletes one thinking it is abandoned work. The prefix is also how the pool
#: tells its own sessions from one you started by hand, so a session that has
#: been renamed is out of the pool by that alone.
#:
#: It differs from the `<spare ` the shell implementation used, deliberately:
#: the two pools ran side by side during the rebuild and had to be invisible to
#: each other, and a leftover `<spare ` session must never be mistaken for one
#: of these.
def name_prefix() -> str:
    return os.environ.get("AWM_CX_PREFIX") or "<warm "


#: Where a session is seeded. It must hold no CLAUDE.md: the destination's file
#: is added on a move and the origin's is not removed, so seeding beside one
#: would carry it into every project the pool serves.
def seed_dir() -> Path:
    return Path(os.environ.get("AWM_CX_SEED_DIR") or Path.home())


def seed_env() -> dict[str, str]:
    return {"ANTHROPIC_MODEL": os.environ.get("AWM_CX_MODEL") or "sonnet[1m]"}


def seed_flags() -> list[str]:
    return [
        "--dangerously-skip-permissions",
        "--allow-dangerously-skip-permissions",
        "--effort", os.environ.get("AWM_CX_EFFORT") or "medium",
    ]


#: Resolved rather than looked up at use: this service runs under systemd, whose
#: PATH is the system default and does not carry ~/.local/bin. A `claude` that
#: is not found there fails silently and the pool stays empty while everything
#: reports healthy.
def claude_bin() -> str:
    env = os.environ.get("AWM_CX_CLAUDE")
    if env:
        return env
    found = shutil.which("claude")
    if found:
        return found
    return str(Path.home() / ".local" / "bin" / "claude")


#: How many sessions to keep warm. Zero stops the pool without stopping the
#: service, which is what a node that does not want it should use.
def want() -> int:
    try:
        return max(0, int(os.environ.get("AWM_CX_WANT") or 1))
    except ValueError:
        return 1


#: A session is replaced once it reaches this age. The daemon retires an idle
#: background session at about 61 minutes, so the limit has to clear that with
#: enough room for the replacement to be seeded and verified before the old one
#: is anywhere near it.
def rotate_age_s() -> float:
    try:
        return float(os.environ.get("AWM_CX_ROTATE_AGE_S") or 2400.0)
    except ValueError:
        return 2400.0


def tick_s() -> float:
    try:
        return float(os.environ.get("AWM_CX_TICK_S") or 5.0)
    except ValueError:
        return 5.0


#: Set to "0" to keep the reconcile loop from running at all. A shadow run of
#: this service uses it so a sandbox does not seed against the same home
#: directory as the base.
def loop_enabled() -> bool:
    return (os.environ.get("AWM_CX_LOOP") or "1") != "0"


def state_dir() -> Path:
    p = Path(os.environ.get("AWM_CX_STATE") or (_home() / "cx"))
    return p
