"""Agent spawning service — fire-and-forget agent subprocesses."""

from __future__ import annotations

import subprocess
from pathlib import Path

from awm.config import MAIN_DIR
from awm.models import (
    AgentSpawnRequest,
    AgentSpawnResponse,
    MessageSendRequest,
)
from awm.services import config_service, messaging

_SUPPORTED_CLIS = {"opencode", "claude"}


def spawn_agent(req: AgentSpawnRequest) -> AgentSpawnResponse:
    """Spawn a fire-and-forget agent on a task workspace."""
    # Resolve CLI
    cli = req.agent_cli or config_service.get_config("agent_cli", "opencode")

    if cli not in _SUPPORTED_CLIS:
        raise ValueError(f"Unknown agent CLI '{cli}'. Supported: {sorted(_SUPPORTED_CLIS)}")

    # Verify task workspace exists
    workspace_dir = MAIN_DIR / req.project / "tasks" / req.task
    if not workspace_dir.exists():
        raise FileNotFoundError(
            f"Task workspace not found at {workspace_dir}"
        )

    # Send prompt as plan message if provided
    if req.prompt:
        msg_req = MessageSendRequest(
            scope=f"task:{req.project}/{req.task}",
            sender="workspace",
            msg_type="plan",
            subject=f"Agent spawn plan for {req.project}/{req.task}",
            body=req.prompt,
        )
        messaging.send_message(msg_req)

    # Build command
    if cli == "opencode":
        cmd = ["opencode"]
    else:  # claude
        prompt = req.prompt or f"Work on task {req.task} in project {req.project}. Check your inbox first."
        cmd = ["claude", "--print", "-p", prompt]

    # Spawn
    log_file = open(workspace_dir / "agent.log", "a")
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
        task=req.task,
        pid=proc.pid,
        agent_cli=cli,
        message=f"Spawned {cli} agent (PID {proc.pid}) in {workspace_dir}",
    )
