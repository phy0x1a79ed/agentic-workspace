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
_UNIT = "awm.service"


def user_unit_is_active(unit: str = _UNIT) -> bool:
    """True when a per-user systemd unit is loaded and running.

    The host may or may not be supervised by systemd, and guessing wrong is not
    a cosmetic error: a gateway started from the PID file and orphaned to init
    is not restartable by ``systemctl``, which reports the unit as not found and
    leaves the process running.

    ``is-active`` is the right predicate — deliberately not ``is-enabled``. A
    unit symlink can be enabled and still be unusable (dangling link, bad unit),
    in which case systemd calls it ``enabled`` but ``bad`` and starting it
    fails. Only "there is a live unit supervising this" justifies handing the
    restart to systemd.
    """
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.stdout.strip() == "active"


def _port_is_free(host: str, port: int) -> bool:
    """True when nothing accepts a connection on ``host:port``."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, int(port))) != 0


def _wait_port_free(host: str, port: int, deadline: float) -> bool:
    """Block until the listen socket is actually released, or the deadline.

    Waiting on "the process is gone" is not enough: the replacement binds the
    same port, and a listener that has exited but not yet released its socket
    makes the new one die on EADDRINUSE — which then looks like a gateway that
    crashed on boot rather than a restart that overlapped itself.
    """
    while time.monotonic() < deadline:
        if _port_is_free(host, port):
            return True
        time.sleep(0.3)
    return _port_is_free(host, port)


def _stop_via_pidfile() -> int | None:
    """SIGTERM the pid recorded in ``PID_FILE``. Returns the pid, or None."""
    import os
    import signal

    from awm.config import PID_FILE

    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # already gone; the pidfile was stale
    except OSError as exc:
        print(f"[awm] restart: could not signal pid={pid}: {exc}", flush=True)
        return None
    PID_FILE.unlink(missing_ok=True)
    return pid


def _start_detached() -> None:
    """Spawn a fresh gateway in its own session, as ``awm gateway serve`` does."""
    import os
    import sys

    from awm.config import AWM_DIR, WORKSPACE_ROOT

    AWM_DIR.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["AWM_WORKSPACE"] = str(WORKSPACE_ROOT)
    with open(AWM_DIR / "awm.log", "a") as log_file:
        subprocess.Popen(
            [sys.executable, "-m", "awm.gateway", "gateway", "serve"],
            stdout=log_file, stderr=log_file, stdin=subprocess.DEVNULL,
            start_new_session=True, env=env,
        )


def restart_core_and_wait(
    timeout: float = 60.0,
    restart_cmd: list[str] | None = None,
) -> dict[str, object]:
    """Restart the gateway and wait until the new process is healthy.

    Works on a host supervised by systemd and on one that is not. Which it is
    gets probed, not assumed: prod on some hosts is started from the PID file
    and orphaned to init, where ``systemctl restart`` reports the unit missing
    and leaves the gateway running.

    Steps:
        1. Pre-flight — record the current process's PID and uptime.
        2. Orphan sweep, then trigger the restart: hand it to systemd, or stop
           via the PID file, wait for the port, and spawn the replacement.
        3. (systemd only) Poll ``/status`` until the old process exits.
        4. Poll ``/status`` until the new process returns ``{"status": "ok"}``
           **and** carries a different PID **and** uptime < 5s (fresh boot).
        5. Return a result dict with timing, the new PID, and ``managed_by``.

    The PID+uptime checks prevent false positives from a stale process holding
    the port or a lingering cached response, and apply on both paths — they are
    the only thing separating "restarted" from "never died".

    The default budget covers draining a full service set; a host running
    twenty-odd services does not finish in the 30s this used to allow.

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

    # An explicit restart_cmd is a caller that KNOWS its supervisor (``awm
    # deploy`` targets the prod system unit), so it always wins — the probe must
    # not collapse per-user and system units into one decision. Otherwise ask
    # systemd whether it is actually supervising this gateway.
    use_systemd = restart_cmd is not None or user_unit_is_active()
    result["managed_by"] = "systemd" if use_systemd else "pidfile"
    drained = False

    if use_systemd:
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
    else:
        # No supervisor: stop it ourselves, wait for the socket to be released,
        # then start the replacement. The drain has to complete BEFORE the new
        # process starts, which is why the generic drain-wait below is skipped —
        # by then the responding gateway is the new one, and waiting for it to
        # stop responding would spin until the deadline.
        from awm.config import HOST, PORT

        signalled = _stop_via_pidfile()
        if signalled is not None:
            result["stopped_pid"] = signalled
        if not _wait_port_free(HOST, PORT, deadline):
            raise _RestartTimeout(
                f"{HOST}:{PORT} still held {timeout:.0f}s after stopping the "
                f"old gateway (pid={signalled})"
            )
        result["drain_s"] = round(time.monotonic() - start, 1)
        drained = True
        _start_detached()

    # --- 3. Wait for old process to exit (connection refused) ---
    if old_pid is not None and not drained:
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
