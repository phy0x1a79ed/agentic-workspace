"""Core lifecycle operations (restart, etc.)."""

from __future__ import annotations

import subprocess

from awm.gateway._process_utils import sweep_orphan_awm_serves


def restart_core() -> dict[str, str]:
    """Restart the AWM core systemd unit (``awm.service``).

    Uses ``Popen`` (non-blocking) so the HTTP response is sent before
    systemctl delivers SIGTERM.  The systemd unit uses ``Restart=on-failure``,
    so a self-SIGTERM would NOT restart — we must go through systemctl.

    Before bouncing the unit, sweep any ``awm serve`` processes outside the
    awm.service cgroup so a stale orphan can't grab :7819 ahead of the new
    instance (inbox #232).
    """
    sweep_reports: list[dict[str, str | int | None]] = []
    try:
        for r in sweep_orphan_awm_serves():
            sweep_reports.append({
                "pid": r.pid, "action": r.action, "detail": r.detail,
            })
            if r.action == "killed":
                print(
                    f"[awm] restart: swept orphan awm serve pid={r.pid} ({r.detail})",
                    flush=True,
                )
    except Exception as exc:  # noqa: BLE001 — sweep is best-effort
        print(f"[awm] restart: orphan sweep skipped: {exc}", flush=True)
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
            "(see awm/gateway/deploy/awm.service) and ensure systemd is available."
        )
    result: dict[str, object] = {
        "status": "restarting",
        "units": "awm.service",
        "message": "restarting awm.service via systemd. MCP clients reconnect on next tool call.",
    }
    if sweep_reports:
        result["orphan_sweep"] = sweep_reports
    return result  # type: ignore[return-value]
