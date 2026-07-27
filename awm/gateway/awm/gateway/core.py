"""Core lifecycle operations (restart, etc.)."""

from __future__ import annotations

import subprocess
import time

import httpx

from awm.config import BASE_URL
from awm.gateway._process_utils import sweep_orphan_awm_serves


def restart_core() -> dict[str, str]:
    """Restart the AWM core systemd unit (``awm.service``).

    Uses ``Popen`` (non-blocking) so the HTTP response is sent before
    systemctl delivers SIGTERM.  The systemd unit uses ``Restart=on-failure``,
    so a self-SIGTERM would NOT restart — we must go through systemctl.

    Before bouncing the unit, sweep any ``awm gateway serve`` processes outside
    the awm.service cgroup so a stale orphan can't grab :7819 ahead of the new
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
                    f"[awm] restart: swept orphan awm gateway serve pid={r.pid} ({r.detail})",
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


class _RestartTimeout(RuntimeError):
    """Raised when restart_core_and_wait fails at a specific phase."""


# The default restart invocation (a per-user unit). ``awm deploy`` overrides
# this with the prod *system* unit command (``sudo -n systemctl restart``) —
# prod runs ``/etc/systemd/system/awm.service``, which a per-user restart cannot
# touch.
_DEFAULT_RESTART_CMD = ["systemctl", "--user", "restart", "awm.service"]


def restart_core_and_wait(
    timeout: float = 30.0,
    restart_cmd: list[str] | None = None,
) -> dict[str, object]:
    """Restart the gateway via systemd and wait until the new process is healthy.

    Steps:
        1. Pre-flight — record the current process's PID and uptime.
        2. Orphan sweep + ``systemctl --user restart awm.service``.
        3. Poll ``/status`` until the old process exits (connection refused).
        4. Poll ``/status`` until the new process returns ``{"status": "ok"}``
           **and** carries a different PID **and** uptime < 5s (fresh boot).
        5. Return a result dict with timing and the new PID.

    The PID+uptime checks prevent false positives from a stale process holding
    the port or a lingering cached response.

    Raises ``_RestartTimeout`` if any phase exceeds *timeout*.
    """
    deadline = time.monotonic() + timeout
    start = time.monotonic()
    result: dict[str, object] = {"status": "restarting"}

    # --- 1. Pre-flight ---
    old_pid: int | None = None
    try:
        with httpx.Client(base_url=BASE_URL, timeout=2) as client:
            r = client.get("/status")
            data = r.json()
            old_pid = data.get("core_pid")
            result["old_pid"] = old_pid
    except httpx.HTTPError:
        pass  # no running gateway — will start fresh

    # --- 2. Sweep + trigger systemctl restart ---
    try:
        for r in sweep_orphan_awm_serves():
            if r.action == "killed":
                print(f"[awm] restart: swept orphan pid={r.pid}", flush=True)
    except Exception as exc:
        print(f"[awm] restart: orphan sweep skipped: {exc}", flush=True)

    try:
        subprocess.Popen(
            list(restart_cmd or _DEFAULT_RESTART_CMD),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "systemctl not found. Install the awm.service unit "
            "(see awm/gateway/deploy/awm.service) and ensure systemd is available."
        )

    # --- 3. Wait for old process to exit (connection refused) ---
    if old_pid is not None:
        while time.monotonic() < deadline:
            try:
                with httpx.Client(base_url=BASE_URL, timeout=1) as client:
                    client.get("/status")
                # still responding — process hasn't exited yet
            except httpx.HTTPError:
                break  # connection refused = old process is gone
            time.sleep(0.3)
        else:
            raise _RestartTimeout(
                f"old gateway pid={old_pid} did not exit within {timeout:.0f}s"
            )
        result["drain_s"] = round(time.monotonic() - start, 1)

    # --- 4. Wait for new process to boot and be healthy ---
    boot_start = time.monotonic()
    new_pid: int | None = None
    while time.monotonic() < deadline:
        try:
            with httpx.Client(base_url=BASE_URL, timeout=2) as client:
                r = client.get("/status")
                data = r.json()
                if data.get("status") == "ok":
                    pid = data.get("core_pid")
                    uptime = data.get("core_uptime_s", 999)
                    if old_pid is not None and pid == old_pid:
                        continue  # same PID — still the old process
                    if uptime > 5:
                        continue  # stale response from lingering old process
                    new_pid = pid
                    break
        except httpx.HTTPError:
            pass
        time.sleep(0.3)

    if new_pid is None:
        raise _RestartTimeout(
            "new gateway process did not become healthy "
            f"within {timeout:.0f}s"
        )

    # --- 5. Report ---
    total = round(time.monotonic() - start, 1)
    result.update({
        "status": "ok",
        "new_pid": new_pid,
        "boot_s": round(time.monotonic() - boot_start, 1),
        "total_s": total,
        "message": f"Gateway restarted: {f'PID {old_pid} → ' if old_pid else ''}"
                   f"{new_pid} ({total}s)",
    })
    return result
