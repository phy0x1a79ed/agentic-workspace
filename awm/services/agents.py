"""Agent spawning service — fire-and-forget agent subprocesses."""

from __future__ import annotations

import subprocess
from pathlib import Path

from awm.config import PROJECTS_DIR
from awm.models import (
    AgentSpawnRequest,
    AgentSpawnResponse,
    MessageSendRequest,
)
from awm.services import config_service, messaging


_SUPPORTED_CLIS = {"opencode", "claude"}


def spawn_agent(req: AgentSpawnRequest) -> AgentSpawnResponse:
    """Spawn a fire-and-forget agent on a scope workspace."""
    # Resolve CLI
    cli = req.agent_cli or config_service.get_config("agent_cli", "opencode")

    if cli not in _SUPPORTED_CLIS:
        raise ValueError(f"Unknown agent CLI '{cli}'. Supported: {sorted(_SUPPORTED_CLIS)}")

    # Verify scope workspace exists (agent lands in the git worktree)
    workspace_dir = PROJECTS_DIR / req.project / req.scope
    if not workspace_dir.exists():
        raise FileNotFoundError(
            f"Scope workspace not found at {workspace_dir}"
        )

    # Send prompt as plan message if provided
    if req.prompt:
        msg_req = MessageSendRequest(
            scope=f"scope:{req.project}/{req.scope}",
            sender="workspace",
            msg_type="plan",
            subject=f"Agent spawn plan for {req.project}/{req.scope}",
            body=req.prompt,
        )
        messaging.send_message(msg_req)

    # Build command
    if cli == "opencode":
        cmd = ["opencode"]
    else:  # claude
        prompt = req.prompt or f"Work on scope {req.scope} in project {req.project}. Read .awm/context.md first."
        cmd = ["claude", "--print", "-p", prompt]

    # Spawn
    awm_dir = workspace_dir / ".awm"
    awm_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(awm_dir / "agent.log", "a")
    proc = subprocess.Popen(
        cmd,
        cwd=str(workspace_dir),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=log_file,
    )

    return AgentSpawnResponse(
        project=req.project,
        scope=req.scope,
        pid=proc.pid,
        agent_cli=cli,
        message=f"Spawned {cli} agent (PID {proc.pid}) in {workspace_dir}",
    )
