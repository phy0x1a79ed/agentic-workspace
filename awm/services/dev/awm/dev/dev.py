"""Core logic for the `dev` service: drive this worktree's dev sandbox.

The six things `awm dev` does (start / stop / restart / seed / status, plus the
manual `shadow` which stays a CLI foreground command — see cli.py) are exposed
as gateway tools. The lifecycle ops here shell out to the worktree's
``awm/gateway/dev/run.sh`` — keeping run.sh as the mechanism; the service simply
*owns* the operations on the bus.

Self-shadow makes routing work without retargeting ``awm``: a dev sandbox's dev
service registers an overlay of ``/svc/dev`` onto prod (see hub_adapter.py), so
``awm dev <op>`` (which targets prod :7819 by default) is served by the
sandbox's worktree code. Because lifecycle is inherently worktree-local, the
handlers only *act* when this process is a dev sandbox's dev service — detected
by ``AWM_SHADOW_HUB_URL`` being set. On a prod base (no shadow target) the
handlers return an inert marker so the CLI falls back to running run.sh locally.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Set by the dev sandbox's run.sh (= prod hub url). Its presence is what makes
# this process a *dev* dev-service rather than a prod base. Read once at import:
# the env is fixed for the process's lifetime by the spawning gateway.
SHADOW_HUB_URL = os.environ.get("AWM_SHADOW_HUB_URL", "").strip()
IS_DEV_SANDBOX = bool(SHADOW_HUB_URL)

_LIFECYCLE_ACTIONS = ("start", "stop", "restart", "seed", "status")


def _dev_run_sh() -> Path:
    """Locate this worktree's dev sandbox script (``awm/gateway/dev/run.sh``).

    Resolved from THIS module's on-disk location — so when the sandbox's overlay
    serves a call it finds the *sandbox* worktree's script, and a prod base would
    find prod's. Walks up from ``…/awm/services/dev/awm/dev/dev.py`` to the
    worktree root, then ``awm/gateway/dev/run.sh``.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "awm" / "gateway" / "dev" / "run.sh"
        if cand.is_file():
            return cand
    raise FileNotFoundError(
        f"awm/gateway/dev/run.sh not found above {here}")


def run_lifecycle(action: str) -> dict:
    """Run ``bash run.sh <action>`` for the worktree, capturing output.

    Returns a JSON-able dict. On a prod base (no ``AWM_SHADOW_HUB_URL``) returns
    an ``inert`` marker instead of acting — lifecycle is worktree-local, so a
    prod base must not start/stop anything; the CLI falls back to ``--local``.
    """
    if action not in _LIFECYCLE_ACTIONS:
        return {"action": action, "rc": 2,
                "error": f"unknown dev action {action!r}"}
    if not IS_DEV_SANDBOX:
        return {
            "action": action,
            "inert": True,
            "reason": "dev lifecycle is only served by a dev sandbox; this is a "
                      "prod base. Run it locally in your worktree instead.",
        }
    run_sh = _dev_run_sh()
    proc = subprocess.run(
        ["bash", str(run_sh), action],
        cwd=str(run_sh.parent),
        capture_output=True,
        text=True,
    )
    return {
        "action": action,
        "rc": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }
