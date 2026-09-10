"""Starting one warm session.

This is the step that can damage the user's work, so read the precondition
before changing anything here.

Every background Claude Code session on a node is a child of the *first*
process that launched one after the reboot — the daemon it started. That daemon
donates its control group and its environment to every session that follows. If
the gateway were ever the process that started it, then `systemctl restart awm`
would kill every session the user is working in, and every session on the box
would come up with an environment that has no `~/.local/bin` on its PATH.

So the pool refuses to seed unless a daemon is already running. No daemon means
nobody is using Claude Code on that node, and a cold first launch is the right
answer there. The transient unit below is defence in depth for the residual
race, where the daemon dies between the check and the launch.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import shutil
import uuid
from pathlib import Path

from awm.cx import config, sessions

log = logging.getLogger("awm.cx.seed")

#: Sessions are named so they read as tooling in `claude agents` and nobody
#: deletes one thinking it is abandoned work.
NOUNS = (
    "otter heron badger vole finch stoat lynx marmot quokka teal ibis civet "
    "tapir egret gannet dunlin serval kestrel oryx saiga jerboa numbat quoll "
    "dingo fossa gerbil marten pika ratel shrew skink tanager vireo weka xerus "
    "yapok zorilla auklet bittern chough"
).split()

#: A launch answers in about four seconds. The ceiling is generous because the
#: cost of giving up early is a second session nobody asked for.
LAUNCH_TIMEOUT_S = 45.0
POLL_S = 0.25


class Refused(Exception):
    """The precondition on seeding was not met."""


def precondition() -> str | None:
    """Why seeding must not happen right now, or None if it may."""
    if config.want() == 0:
        return "the pool is switched off (AWM_CX_WANT=0)"
    if sessions.daemon_pid() is None:
        return ("no claude code daemon is running — seeding now would put every "
                "background session on this node inside awm's control group")
    if sessions.binary_version() is None:
        return f"no claude binary at {config.claude_bin()}"
    seed_dir = config.seed_dir()
    if (seed_dir / "CLAUDE.md").exists():
        return (f"the seed directory {seed_dir} holds a CLAUDE.md, which every "
                "session moved out of it would carry into someone's project")
    return None


async def seed_one() -> str:
    """Start one warm session and return its short id.

    Raises `Refused` if the precondition fails, `TimeoutError` if no new
    session appears. The exit status of the launch is deliberately not trusted:
    the session is the daemon's child, not the launcher's, so the only proof
    that one exists is a new record in the roster.
    """
    refusal = precondition()
    if refusal:
        raise Refused(refusal)

    before = {s.short for s in sessions.load()}
    name = f"{config.name_prefix()}{random.choice(NOUNS)}>"
    argv, how, extra_env = _launch_argv(name)
    log.info("cx: seeding %s %s", name, how)

    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(config.seed_dir()),
        env={**os.environ, **config.seed_env(), **extra_env,
             "HOME": str(Path.home())},
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=LAUNCH_TIMEOUT_S)
    except (TimeoutError, asyncio.TimeoutError):
        proc.kill()

    version = sessions.binary_version()
    deadline = asyncio.get_running_loop().time() + LAUNCH_TIMEOUT_S
    while asyncio.get_running_loop().time() < deadline:
        for s in sessions.load():
            if s.short not in before and sessions.claimable(s, version=version):
                log.info("cx: seeded %s as %s", s.name, s.short)
                return s.short
        await asyncio.sleep(POLL_S)
    raise TimeoutError(f"no new warm session appeared within {LAUNCH_TIMEOUT_S:.0f}s")


def _launch_argv(name: str) -> tuple[list[str], str, dict[str, str]]:
    """The command that starts one session, how to describe it, and the env
    the command itself needs.

    `KillMode=process` is the property that matters. A transient unit torn down
    the default way kills its whole control group, and in the race this unit
    exists to cover — the daemon having just died — that group holds the fresh
    daemon and the session it was launching. That is exactly the defect that had
    one node's timer seed a session and kill it 0.3 seconds later, silently,
    every fifteen minutes.
    """
    claude = [config.claude_bin(), "--bg", "-n", name, *config.seed_flags()]
    bus = _user_manager_env()
    if not bus:
        return claude, "directly (no user systemd manager)", {}
    unit = f"awm-cx-seed-{uuid.uuid4().hex[:8]}"
    return [
        "systemd-run", "--user", "--quiet", "--collect", f"--unit={unit}",
        "--property=Restart=no", "--property=KillMode=process", "--nice=19",
        *(f"--setenv={k}={v}" for k, v in config.seed_env().items()),
        "--", *claude,
    ], f"into user unit {unit}.service", bus


def _user_manager_env() -> dict[str, str] | None:
    """Env for reaching this user's systemd manager, or None if there is none."""
    if not shutil.which("systemd-run"):
        return None
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    if not Path(runtime, "bus").is_socket():
        return None
    return {
        "XDG_RUNTIME_DIR": runtime,
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus",
    }
