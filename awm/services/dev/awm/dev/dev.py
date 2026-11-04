"""Core logic for the `dev` service: drive a worktree's dev sandbox.

Each lifecycle op shells out to ``<worktree>/awm/services/dev/sandbox/run.sh``,
which runs that worktree's Rust gateway on the worktree's own port band. The
caller names the worktree: the service runs in prod's tree, and the sandbox it
drives is someone else's.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

LIFECYCLE_ACTIONS = ("start", "stop", "restart", "seed", "status")
SANDBOX = Path("awm") / "services" / "dev" / "sandbox" / "run.sh"


def sandbox_script(worktree: str) -> Path:
    """The sandbox script of ``worktree``, an absolute path under the workspace's ``projects/``.

    Raises ``ValueError`` for anything else, so a call cannot point the service
    at a script outside the workspace's worktrees.
    """
    if not worktree or not os.path.isabs(worktree):
        raise ValueError(f"worktree must be an absolute path, got {worktree!r}")
    root = Path(worktree).resolve()
    projects = (Path(os.environ.get("AWM_WORKSPACE") or Path.home() / "agentic_workspace") / "projects").resolve()
    if projects not in root.parents:
        raise ValueError(f"worktree {root} is not under {projects}")
    script = root / SANDBOX
    if not script.is_file():
        raise ValueError(f"no dev sandbox at {script}")
    return script


def run_lifecycle(action: str, worktree: str) -> dict:
    """Run ``bash run.sh <action>`` for ``worktree``'s sandbox, capturing output."""
    if action not in LIFECYCLE_ACTIONS:
        return {"action": action, "rc": 2, "error": f"unknown dev action {action!r}"}
    try:
        script = sandbox_script(worktree)
    except ValueError as exc:
        return {"action": action, "rc": 2, "error": str(exc)}
    proc = subprocess.run(
        ["bash", str(script), action],
        cwd=str(script.parent),
        capture_output=True,
        text=True,
    )
    return {"action": action, "rc": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
