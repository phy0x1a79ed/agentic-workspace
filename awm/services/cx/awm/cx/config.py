"""Where the pool reads and writes, and the knobs that shape it.

Every value is overridable from the environment so a shadow run can be aimed
somewhere harmless, and so the tests never touch the real home directory.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from awm.claudedaemon import sessionmode

# The five paths below are resolved by `claudedaemon.sessionmode`, which the
# gateway reads with no cx process in between. One definition keeps the writer
# and the gateway's reader pointed at the same lineage directory.


#: The daemon's roster of background sessions. Liveness, the PTY lane, the CLI
#: version and the start time come from here; nothing about identity does.
def roster_path() -> Path:
    return sessionmode.roster_path()


#: One directory per session, holding the session's own state record. This is
#: the only source of identity: whether a session has been renamed, prompted or
#: moved.
def jobs_dir() -> Path:
    return sessionmode.jobs_dir()


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


#: What a session is renamed to the moment a terminal takes it. The angle
#: brackets go with the name: a spare is furniture and reads as furniture, a
#: claimed session belongs to somebody and reads as a plain name. The rename is
#: also what lets the built-in namer replace it later, because Claude Code only
#: titles a session whose state record carries no name at all.
def claimed_prefix() -> str:
    return os.environ.get("AWM_CX_CLAIMED_PREFIX") or "claimed "


#: Where a session is seeded. It must hold no CLAUDE.md: the destination's file
#: is added on a move and the origin's is not removed, so seeding beside one
#: would carry it into every project the pool serves.
def seed_dir() -> Path:
    return Path(os.environ.get("AWM_CX_SEED_DIR") or Path.home())


def default_model() -> str:
    return os.environ.get("AWM_CX_MODEL") or "sonnet[1m]"


def default_effort() -> str:
    return os.environ.get("AWM_CX_EFFORT") or "medium"


def skip_permission_flags() -> list[str]:
    return ["--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"]


def seed_env() -> dict[str, str]:
    return {"ANTHROPIC_MODEL": default_model()}


def seed_flags() -> list[str]:
    return [*skip_permission_flags(), "--effort", default_effort()]


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
    return sessionmode.state_dir()


#: One JSON file per session `start` created, named for the job. It is the only
#: record of who started a session and in what mode: `stop` acts on a job only
#: when it has one, and `mode_of` reads the mode from it.
def starts_dir() -> Path:
    return sessionmode.starts_dir()


#: Claude Code's per-process session records, which name every live REPL,
#: interactive or background.
def sessions_dir() -> Path:
    return sessionmode.sessions_dir()


#: Where scope worktrees live: `<canonical workspace>/projects/<project>/<scope>`.
def projects_dir() -> Path:
    env = os.environ.get("AWM_CX_PROJECTS")
    if env:
        return Path(env)
    from awm import config as awm_config

    return awm_config.canonical_workspace() / "projects"


#: The built-in tools a delegate holds: file work inside its scope, and the
#: tools that talk to other agents. There is no Bash, so it runs nothing.
DELEGATE_TOOLS = ["Read", "Grep", "Glob", "Edit", "Write", "MultiEdit", "TodoWrite",
                  "Task", "Agent", "SendMessage", "ListAgents", "Skill", "ToolSearch"]


def child_policy() -> dict:
    """What cx imposes on a session that a gated session started.

    `dontAsk` denies at once anything that would prompt, so an unwatched
    delegate cannot stall. `restricted` confines the file tools to the scope's
    worktree and ignores user settings, and the strict MCP config leaves it only
    the awm server.
    """
    from awm.config import modes

    return {
        "mode": modes.DELEGATE, "permission": "dontAsk",
        "tools": list(DELEGATE_TOOLS),
        "allowed_tools": [*DELEGATE_TOOLS, "mcp__awm__*"],
        "disallowed_tools": ["Bash", "PowerShell", "Monitor", "Workflow", "WebFetch",
                             "WebSearch", "NotebookEdit"],
        "restricted": True, "strict_mcp": True, "remote_control": None,
    }


def mcp_source() -> Path:
    """The workspace's own MCP config, which names how to start the awm server."""
    env = os.environ.get("AWM_CX_MCP_SOURCE")
    if env:
        return Path(env)
    from awm import config as awm_config

    return awm_config.canonical_workspace() / ".mcp.json"


def awm_mcp_server() -> dict | None:
    """The `awm` server entry of the workspace MCP config, or None if it has none."""
    import json

    data = json.loads(mcp_source().read_text())
    server = (data.get("mcpServers") or {}).get("awm") if isinstance(data, dict) else None
    if not isinstance(server, dict) or not isinstance(server.get("command"), str):
        return None
    return {k: server[k] for k in ("type", "command", "args", "env") if k in server}


def mcp_config_path() -> Path:
    return state_dir() / "mcp-awm.json"
