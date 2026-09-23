"""Lifecycle control for the webui process, bridged across a Linux user boundary.

The actual checkout, venv and generated content live under a separate,
permission-locked OS account (``u1111``'s home is mode 700) so that neither
this service's own process nor an ordinary filesystem sweep run as the
gateway's user can read into it. What makes this service able to act on that
process anyway is a narrow ``sudoers.d`` rule (``/etc/sudoers.d/awm-1111``)
that lets the gateway's user run exactly one fixed script
(``/home/u1111/bin/1111ctl.sh``) as ``u1111``, with a fixed list of
arguments and no wildcard — so this bridge can start/stop/restart/check the
webui and the gallery viewer, and nothing else about that account's files.

This module only ever shells out through that bridge. It never reads or
writes anything under the other account's home directory directly.
"""

from __future__ import annotations

import subprocess
from typing import Any

CONTROL_USER = "u1111"
CONTROL_SCRIPT = "/home/u1111/bin/1111ctl.sh"
_TIMEOUT_S = {"status": 10, "view-status": 10, "restart": 45, "view-restart": 45}

# Must match WEBUI_PORT and VIEW_PORT in deploy/1111ctl.sh.
WEBUI_PORT = 17860
VIEW_PORT = 17861


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
    return _with_running(_run("status"))


def view_start() -> dict[str, Any]:
    return _run("view-start")


def view_stop() -> dict[str, Any]:
    return _run("view-stop")


def view_restart() -> dict[str, Any]:
    return _run("view-restart")


def view_status() -> dict[str, Any]:
    return _with_running(_run("view-status"))


def _with_running(result: dict[str, Any]) -> dict[str, Any]:
    result["running"] = result["rc"] == 0
    return result
