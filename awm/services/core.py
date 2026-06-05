"""Core lifecycle operations (restart, etc.)."""

from __future__ import annotations

import subprocess


def restart_core() -> dict[str, str]:
    """Restart the AWM core systemd unit (``awm.service``).

    Uses ``Popen`` (non-blocking) so the HTTP response is sent before
    systemctl delivers SIGTERM.  The systemd unit uses ``Restart=on-failure``,
    so a self-SIGTERM would NOT restart — we must go through systemctl.
    """
    try:
        subprocess.Popen(
            ["systemctl", "--user", "restart", "awm.service"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "systemctl not found. Install the awm.service unit "
            "(see deploy/awm.service) and ensure systemd is available."
        )
    return {
        "status": "restarting",
        "units": "awm.service",
        "message": "restarting awm.service via systemd. MCP clients reconnect on next tool call.",
    }
