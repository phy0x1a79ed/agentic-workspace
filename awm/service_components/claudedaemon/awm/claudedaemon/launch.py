"""Starting one background Claude Code session.

This is the step that can damage the user's work, so read the precondition
before changing anything here.

Every background Claude Code session on a node is a child of the *first*
process that launched one after the reboot — the daemon it started. That daemon
donates its control group and its environment to every session that follows. If
the gateway were ever the process that started it, then `systemctl restart awm`
would kill every session the user is working in, and every session on the box
would come up with an environment that has no `~/.local/bin` on its PATH.

So a launch is refused unless a daemon is already running. No daemon means
nobody is using Claude Code on that node, and a cold first launch is the right
answer there. The transient unit below is defence in depth for the residual
race, where the daemon dies between the check and the launch.

This module decides nothing about which session to start or whether the caller
may start it. It takes the directory, name, flags, environment and prompt, and
returns the session the daemon registered.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Callable

from awm.claudedaemon import roster

log = logging.getLogger("awm.claudedaemon.launch")

#: A launch answers in about four seconds. The ceiling is generous because the
#: cost of giving up early is a second session nobody asked for.
LAUNCH_TIMEOUT_S = 45.0
POLL_S = 0.25


class Refused(Exception):
    """The precondition on launching was not met."""


def daemon_refusal(roster_path: Path | None = None) -> str | None:
    """Why a launch must not happen right now, or None if it may.

    With no user manager the launch runs straight from this process, and it
    joins the running daemon only if it can see that daemon's sockets. A
    gateway under a hardened unit has a mount namespace of its own, so its
    launch starts a second daemon inside its own control group, which takes
    the user's sessions down with the next restart.
    """
    pid = roster.daemon_pid(roster_path)
    if pid is None:
        return ("no claude code daemon is running — launching now would put every "
                "background session on this node inside awm's control group")
    if user_manager_env() is None and not _shares_mount_namespace(pid):
        return (f"the claude code daemon (pid {pid}) is not reachable from here: "
                "there is no user systemd manager, and the daemon runs in another "
                "mount namespace — launching now would start a second daemon "
                "inside awm's control group")
    return None


def _shares_mount_namespace(pid: int) -> bool:
    """Whether ``pid`` shares this process's mount namespace; unreadable is no."""
    try:
        return os.readlink(f"/proc/{pid}/ns/mnt") == os.readlink("/proc/self/ns/mnt")
    except OSError:
        return False


def default_claude_bin() -> str:
    found = shutil.which("claude")
    if found:
        return found
    return str(Path.home() / ".local" / "bin" / "claude")


def user_manager_env() -> dict[str, str] | None:
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


def build_argv(*, claude: str, name: str, flags: list[str] | tuple[str, ...],
               cwd: str | Path, env: dict[str, str],
               prompt: str | None = None,
               unit_prefix: str = "awm-claude",
               ) -> tuple[list[str], str, dict[str, str]]:
    """The command that starts one session, how to describe it, and the env
    the command itself needs.

    `KillMode=process` is the property that matters. A transient unit torn down
    the default way kills its whole control group, and in the race this unit
    exists to cover — the daemon having just died — that group holds the fresh
    daemon and the session it was launching. That is exactly the defect that had
    one node's timer seed a session and kill it 0.3 seconds later, silently,
    every fifteen minutes.

    The prompt goes after a `--`. Without it the CLI drops the prompt silently
    and the session starts empty.
    """
    cmd = [claude, "--bg", f"--name={name}", *flags]
    if prompt:
        cmd += ["--", prompt]
    return in_unit(cmd, cwd=cwd, env=env, unit_prefix=unit_prefix)


def in_unit(cmd: list[str], *, cwd: str | Path, env: dict[str, str],
            unit_prefix: str) -> tuple[list[str], str, dict[str, str]]:
    """``cmd`` wrapped in a transient user unit when there is a user manager."""
    bus = user_manager_env()
    if not bus:
        return cmd, "directly (no user systemd manager)", {}
    unit = f"{unit_prefix}-{uuid.uuid4().hex[:8]}"
    return [
        "systemd-run", "--user", "--quiet", "--collect", f"--unit={unit}",
        "--property=Restart=no", "--property=KillMode=process", "--nice=19",
        f"--working-directory={cwd}",
        *(f"--setenv={k}={v}" for k, v in env.items()),
        "--", *cmd,
    ], f"into user unit {unit}.service", bus


async def launch(*, cwd: str | Path, name: str,
                 flags: list[str] | tuple[str, ...] = (),
                 env: dict[str, str] | None = None,
                 prompt: str | None = None,
                 claude: str | None = None,
                 unit_prefix: str = "awm-claude",
                 roster_path: Path | None = None,
                 jobs_dir: Path | None = None,
                 accept: Callable[[roster.Session], bool] | None = None,
                 label: str = "background",
                 timeout: float | None = None) -> roster.Session:
    """Start one background session and return the roster's record of it.

    Raises `Refused` if no daemon is running, `TimeoutError` if no new session
    appears. The exit status of the launch is deliberately not trusted: the
    session is the daemon's child, not the launcher's, so the only proof that
    one exists is a new record in the roster. ``accept`` narrows which new
    record counts.
    """
    refusal = daemon_refusal(roster_path)
    if refusal:
        raise Refused(refusal)

    limit = LAUNCH_TIMEOUT_S if timeout is None else timeout
    before = {s.short for s in roster.load(roster_path, jobs_dir)}
    env = dict(env or {})
    argv, how, extra_env = build_argv(
        claude=claude or default_claude_bin(), name=name, flags=flags, cwd=cwd,
        env=env, prompt=prompt, unit_prefix=unit_prefix)
    log.info("claudedaemon: launching %s %s", name, how)

    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env={**os.environ, **env, **extra_env, "HOME": str(Path.home())},
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=limit)
    except (TimeoutError, asyncio.TimeoutError):
        proc.kill()

    deadline = asyncio.get_running_loop().time() + limit
    while asyncio.get_running_loop().time() < deadline:
        for s in roster.load(roster_path, jobs_dir):
            if s.short not in before and (accept is None or accept(s)):
                return s
        await asyncio.sleep(POLL_S)
    raise TimeoutError(f"no new {label} session appeared within {limit:.0f}s")


def _alive(s: roster.Session) -> bool:
    return bool(s.repl_proc_start) and roster.proc_start(s.repl_pid) == s.repl_proc_start


async def respawn(job: str, *, claude: str | None = None,
                  unit_prefix: str = "awm-claude",
                  roster_path: Path | None = None,
                  jobs_dir: Path | None = None,
                  timeout: float | None = None) -> roster.Session:
    """Bring a stopped background job back under its own id, conversation intact.

    `claude respawn` keeps the job id, the transcript and the launch flags.
    `claude --bg --resume <session>` does not: it starts a new, empty job. A
    respawn of a live job restarts it, so a live job is refused. Raises
    `Refused` for that or for a missing daemon, `TimeoutError` if the job does
    not come back live.
    """
    refusal = daemon_refusal(roster_path)
    if refusal:
        raise Refused(refusal)
    before = next((s for s in roster.load(roster_path, jobs_dir) if s.short == job), None)
    if before is not None and _alive(before):
        raise Refused(f"job {job} is running")
    stale = before.repl_proc_start if before else None

    limit = LAUNCH_TIMEOUT_S if timeout is None else timeout
    home = Path.home()
    argv, how, extra_env = in_unit([claude or default_claude_bin(), "respawn", job],
                                   cwd=home, env={}, unit_prefix=unit_prefix)
    log.info("claudedaemon: respawning %s %s", job, how)
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(home), env={**os.environ, **extra_env, "HOME": str(home)},
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(proc.wait(), timeout=limit)
    except (TimeoutError, asyncio.TimeoutError):
        proc.kill()

    deadline = asyncio.get_running_loop().time() + limit
    while asyncio.get_running_loop().time() < deadline:
        for s in roster.load(roster_path, jobs_dir):
            if s.short == job and s.repl_proc_start != stale and _alive(s):
                return s
        await asyncio.sleep(POLL_S)
    raise TimeoutError(f"job {job} did not come back within {limit:.0f}s")
