"""Core lifecycle operations (restart, etc.)."""

from __future__ import annotations

import subprocess

_UNIT_FOR_TARGET = {
    "core": ["awm.service"],
    "exposed": ["awm-exposed.service"],
    "all": ["awm.service", "awm-exposed.service"],
}


def restart_core(target: str = "all") -> dict[str, str]:
    """Restart one or more AWM systemd units.

    ``target`` is ``"core"`` (awm.service only), ``"exposed"``
    (awm-exposed.service only), or ``"all"`` (default: both).

    Uses ``Popen`` (non-blocking) so the HTTP response is sent before
    systemctl delivers SIGTERM.  The systemd unit uses ``Restart=on-failure``,
    so a self-SIGTERM would NOT restart — we must go through systemctl.
    """
    if target not in _UNIT_FOR_TARGET:
        raise ValueError(
            f"unknown restart target {target!r}; "
            f"choices: {sorted(_UNIT_FOR_TARGET)}"
        )
    units = _UNIT_FOR_TARGET[target]
    try:
        subprocess.Popen(
            ["systemctl", "--user", "restart", *units],
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
        "target": target,
        "units": ",".join(units),
        "message": (
            f"restarting {','.join(units)} via systemd. "
            "MCP clients reconnect on next tool call."
        ),
    }
