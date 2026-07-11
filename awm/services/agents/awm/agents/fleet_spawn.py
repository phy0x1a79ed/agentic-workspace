"""Fleet spawn: launch a *plain* interactive agent in a detached tmux session.

This is deliberately **not** a DAG placement. The fleet page's "new agent"
overlay wants exactly what a human would get by opening a terminal at a scope
worktree and typing ``claude`` — no orchestrator task, no workspace unit, no
supervision driver, no one-live-per-scope guard. So this module just:

1. ``tmux new-session -d`` a detached session at ``cwd`` (any path — a scope
   worktree or an arbitrary directory), and
2. ``send-keys`` the harness command (``claude --model … [--effort …]``) so the
   real TUI comes up idle at its prompt.

Because it's a plain ``claude`` in a normal tmux pane, two things fall out for
free: the **global** ``~/.claude/settings.json`` notifications hook fires, so the
new agent appears in the machine-wide roster on its own (with its tmux session
name captured → *attachable*); and the workspace-root ``.mcp.json`` gives it the
awm MCP without any per-spawn config synthesis. The fleet terminal attaches to
it by tmux session name via ``terminal_session``'s ad-hoc resolver; disposal is a
plain ``tmux kill-session`` (or Ctrl-C×2 from the terminal).
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import uuid
from typing import Optional

_VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
_SUPPORTED_HARNESSES = {"claude", "opencode"}

# tmux target names treat '.' and ':' specially and dislike whitespace.
_NAME_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


def _safe_name(base: str) -> str:
    return _NAME_SAFE.sub("-", base).strip("-") or "agent"


def _unique_session_name(cwd: str, tmux_bin: str) -> str:
    """A tmux-safe, collision-free session name rooted at the dir basename."""
    base = _safe_name(os.path.basename(os.path.abspath(cwd.rstrip("/"))) or "agent")
    for _ in range(50):
        name = f"fleet-{base}-{uuid.uuid4().hex[:4]}"
        exists = subprocess.run(
            [tmux_bin, "has-session", "-t", f"={name}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
        if not exists:
            return name
    # Astronomically unlikely; fall back to a full uuid.
    return f"fleet-{uuid.uuid4().hex}"


def build_command(
    harness: str,
    model: Optional[str],
    effort: Optional[str],
    permission: str = "default",
) -> str:
    """The shell command sent into the pane to launch the idle TUI.

    ``claude`` renders ``--permission-mode`` / ``--model`` / ``--effort`` exactly
    as the awm claude backend does. ``opencode`` comes up bare (its model is
    chosen in-app); ``model``/``effort`` are claude-only.
    """
    if harness == "opencode":
        return "opencode"
    argv = ["claude"]
    if permission == "full":
        argv.append("--dangerously-skip-permissions")
    else:
        argv.extend(["--permission-mode", "default"])
    if model:
        argv.extend(["--model", model])
    if effort:
        argv.extend(["--effort", effort])
    return " ".join(shlex.quote(a) for a in argv)


def spawn_terminal(
    *,
    cwd: str,
    harness: str = "claude",
    model: Optional[str] = None,
    effort: Optional[str] = None,
    permission: str = "default",
) -> dict:
    """Launch an idle ``harness`` TUI in a fresh detached tmux session.

    Returns ``{tmux_session, cwd, harness, model, effort, command}``. Raises
    ``ValueError`` / ``FileNotFoundError`` on bad input so the overlay can show a
    clean message.
    """
    harness = (harness or "claude").strip()
    if harness not in _SUPPORTED_HARNESSES:
        raise ValueError(
            f"Unknown harness {harness!r}; expected one of "
            f"{sorted(_SUPPORTED_HARNESSES)}")
    cwd = os.path.abspath(os.path.expanduser((cwd or "").strip()))
    if not cwd or not os.path.isdir(cwd):
        raise FileNotFoundError(f"spawn cwd is not a directory: {cwd!r}")

    model = (model or "").strip() or None
    effort = (effort or "").strip() or None
    if effort and effort not in _VALID_EFFORTS:
        raise ValueError(
            f"Invalid effort {effort!r}; choices: {sorted(_VALID_EFFORTS)}")
    # claude carries an explicit model (no ambient inheritance); opencode picks
    # its model in-app, so a blank model is only rejected for claude.
    if harness == "claude" and not model:
        raise ValueError("claude spawn requires an explicit model")

    tmux_bin = shutil.which("tmux")
    if not tmux_bin:
        raise FileNotFoundError("tmux not found on PATH")
    if not shutil.which(harness):
        raise FileNotFoundError(f"{harness} not found on PATH")

    name = _unique_session_name(cwd, tmux_bin)
    command = build_command(harness, model, effort, permission)

    # Detached session at cwd, then type the launch command + Enter. Keeping the
    # command as a send-keys (not the new-session shell arg) means the pane
    # survives the TUI exiting, so a crash/quit leaves an inspectable shell.
    subprocess.run(
        [tmux_bin, "new-session", "-d", "-s", name, "-c", cwd,
         "-x", "220", "-y", "50"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    subprocess.run(
        [tmux_bin, "send-keys", "-t", name, command, "Enter"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    return {
        "tmux_session": name,
        "cwd": cwd,
        "harness": harness,
        "model": model,
        "effort": effort,
        "command": command,
    }


def kill_tmux_session(name: str) -> bool:
    """Dispose an ad-hoc tmux session by name. True if it was there and killed."""
    tmux_bin = shutil.which("tmux")
    if not tmux_bin or not name:
        return False
    return subprocess.run(
        [tmux_bin, "kill-session", "-t", f"={name}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0
