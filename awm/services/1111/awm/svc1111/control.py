"""Lifecycle control for the webui process, bridged across a Linux user boundary.

The actual checkout, venv and generated content live under a separate,
permission-locked OS account (``u1111``'s home is mode 700) so that neither
this service's own process nor an ordinary filesystem sweep run as the
gateway's user can read into it. What makes this service able to act on that
process anyway is a narrow ``sudoers.d`` rule (``/etc/sudoers.d/awm-1111``)
that lets the gateway's user run exactly one fixed script
(``/home/u1111/bin/1111ctl.sh``) as ``u1111``, with four fixed arguments and
no wildcard — so this bridge can start/stop/restart/check the process, and
nothing else about that account's files.

This module only ever shells out through that bridge. It never reads or
writes anything under the other account's home directory directly.
"""

from __future__ import annotations

import subprocess
from typing import Any

CONTROL_USER = "u1111"
CONTROL_SCRIPT = "/home/u1111/bin/1111ctl.sh"
_TIMEOUT_S = {"start": 30, "stop": 30, "status": 10, "restart": 45}

# Must match PORT in /home/u1111/bin/1111ctl.sh.
WEBUI_PORT = 17860


def _run(action: str) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["sudo", "-n", "-u", CONTROL_USER, CONTROL_SCRIPT, action],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S.get(action, 30),
        )
    except subprocess.TimeoutExpired:
        return {"action": action, "rc": -1, "error": "timed out"}
    return {
        "action": action,
        "rc": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def start() -> dict[str, Any]:
    return _run("start")


def stop() -> dict[str, Any]:
    return _run("stop")


def restart() -> dict[str, Any]:
    return _run("restart")


def status() -> dict[str, Any]:
    result = _run("status")
    result["running"] = result["rc"] == 0
    return result
