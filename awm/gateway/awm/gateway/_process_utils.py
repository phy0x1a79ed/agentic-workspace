"""Process-level utilities: pre-bind probe + orphan sweep.

Two helpers used to harden ``awm.service`` against the orphan-holds-port
restart-loop pattern surfaced by inbox #232:

- ``probe_existing_awm()`` — called from ``awm gateway serve`` before the
  uvicorn bind. If 7819 already answers ``/status`` with a healthy AWM payload,
  exit cleanly so systemd considers the start a success. If 7819 is bound
  but answers with garbage, log the holder PID and exit 1 so the
  ``Errno 98`` restart loop becomes a one-shot diagnostic.

- ``sweep_orphan_awm_serves()`` — called from ``restart_core()`` before
  ``systemctl --user restart awm.service``. Kills any ``awm gateway serve``
  process that isn't a member of the ``awm.service`` cgroup, so a stale
  orphan can't hold the port across the restart.

No ``psutil`` dependency — reuses ``subprocess`` + ``/proc`` parsing as
already established in ``awm/mcp_server.py``.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import NamedTuple


_PROBE_TIMEOUT_S = 1.5
_KILL_GRACE_S = 2.0


class ProbeResult(NamedTuple):
    state: str  # "free" | "healthy_peer" | "foreign_holder"
    holder_pid: int | None
    holder_cmd: str | None
    detail: str | None


def probe_existing_awm(host: str, port: int, expected_workspace_root: str) -> ProbeResult:
    """Probe ``host:port`` to determine whether to proceed with a uvicorn bind.

    Returns:
        ``ProbeResult("free", ...)`` if the port is free — caller should
        proceed with the bind.

        ``ProbeResult("healthy_peer", ...)`` if another AWM is already
        serving the same workspace_root — caller should exit 0 so systemd
        records a success.

        ``ProbeResult("foreign_holder", pid, cmd, detail)`` if the port is
        bound but the holder is not a healthy peer — caller should log
        ``detail`` and exit non-zero.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(_PROBE_TIMEOUT_S)
    try:
        sock.connect((host, port))
    except (ConnectionRefusedError, OSError):
        return ProbeResult("free", None, None, None)
    finally:
        sock.close()

    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/status", timeout=_PROBE_TIMEOUT_S
        ) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status_code = resp.status
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        return _foreign_holder(host, port, f"port bound but /status unreachable: {exc}")

    if status_code != 200:
        return _foreign_holder(host, port, f"/status returned HTTP {status_code}")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return _foreign_holder(host, port, "/status did not return JSON")

    if not (
        isinstance(payload, dict)
        and payload.get("status") == "ok"
        and isinstance(payload.get("workspace_root"), str)
        and isinstance(payload.get("active_scopes"), int)
    ):
        return _foreign_holder(host, port, "/status payload missing expected fields")

    peer_root = payload["workspace_root"]
    if peer_root != expected_workspace_root:
        return _foreign_holder(
            host, port,
            f"/status workspace_root={peer_root!r} != expected {expected_workspace_root!r}",
        )

    return ProbeResult("healthy_peer", None, None, peer_root)


def _foreign_holder(host: str, port: int, detail: str) -> ProbeResult:
    pid, cmd = _find_port_holder(host, port)
    return ProbeResult("foreign_holder", pid, cmd, detail)


def _find_port_holder(host: str, port: int) -> tuple[int | None, str | None]:
    """Best-effort: identify the PID listening on ``host:port`` via ``ss``."""
    try:
        out = subprocess.run(
            ["ss", "-lntp", "-H"],
            capture_output=True, text=True, timeout=2.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None, None
    if out.returncode != 0:
        return None, None
    needle = f"{host}:{port}"
    for line in out.stdout.splitlines():
        if needle not in line:
            continue
        # users:(("awm",pid=264,fd=13))
        pid_marker = "pid="
        idx = line.find(pid_marker)
        if idx == -1:
            continue
        rest = line[idx + len(pid_marker):]
        digits = ""
        for ch in rest:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            continue
        pid = int(digits)
        cmd = _read_cmdline(pid)
        return pid, cmd
    return None, None


def _read_cmdline(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return None
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip() or None


def exit_if_healthy_peer(host: str, port: int, expected_workspace_root: str) -> None:
    """Run the pre-bind probe; exit the process if appropriate.

    - Healthy peer → log + ``sys.exit(0)``
    - Foreign holder → log + ``sys.exit(1)``
    - Free → return (caller proceeds with the bind)
    """
    result = probe_existing_awm(host, port, expected_workspace_root)
    if result.state == "free":
        return
    if result.state == "healthy_peer":
        print(
            f"[awm] another healthy awm already serving on {host}:{port} "
            f"(workspace_root={result.detail!r}); exiting 0",
            flush=True,
        )
        sys.exit(0)
    # foreign_holder
    holder = (
        f"pid={result.holder_pid} cmd={result.holder_cmd!r}"
        if result.holder_pid is not None
        else "holder unknown"
    )
    print(
        f"[awm] port {host}:{port} held by {holder}; refusing to start ({result.detail})",
        flush=True,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Orphan sweep
# ---------------------------------------------------------------------------


def _read_cgroup(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            return f.read()
    except OSError:
        return None


def _systemd_main_pid() -> int | None:
    """Authoritative PID for the awm.service unit, via systemctl."""
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", "-p", "MainPID", "awm.service"],
            capture_output=True, text=True, timeout=2.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    # Output: "MainPID=<n>"
    line = out.stdout.strip()
    if not line.startswith("MainPID="):
        return None
    try:
        pid = int(line.split("=", 1)[1])
    except ValueError:
        return None
    return pid if pid > 0 else None


def _is_in_awm_service_cgroup(pid: int) -> bool:
    cg = _read_cgroup(pid)
    if not cg:
        return False
    return "/awm.service" in cg


def _is_awm_serve_cmdline(cmdline: str) -> bool:
    """Strict match: an ``awm gateway serve`` invocation, not awm-mcp / other.

    /proc cmdline arrives with NULs already replaced by spaces in
    ``_read_cmdline``. We only sweep the gateway server — the ``awm`` (or
    ``awm.gateway`` under ``python -m``) token followed by ``gateway serve``.
    The two real launch shapes are the systemd unit's ``…/bin/awm gateway
    serve`` and the CLI auto-start's ``python -m awm.gateway gateway serve``;
    both have an ``awm``-ish token immediately preceding ``gateway serve``.
    ``awm-mcp`` proxies (a loose ``awm`` glob) never carry ``gateway serve``.
    """
    parts = cmdline.split()
    for i, tok in enumerate(parts[:-2]):
        base = tok.rsplit("/", 1)[-1]
        if base in ("awm", "awm.gateway") and parts[i + 1:i + 3] == ["gateway", "serve"]:
            return True
    return False


class OrphanReport(NamedTuple):
    pid: int
    cmdline: str
    action: str  # "skipped:self" | "skipped:in_cgroup" | "killed" | "failed"
    detail: str | None


def sweep_orphan_awm_serves(self_pid: int | None = None) -> list[OrphanReport]:
    """Kill ``awm gateway serve`` processes outside the awm.service cgroup.

    Returns a list of per-PID reports for logging. Best-effort: a failure
    in any single step short-circuits to a skip rather than raising — the
    caller (``restart_core``) should not abort the restart on sweep failure.
    """
    if self_pid is None:
        self_pid = os.getpid()

    reports: list[OrphanReport] = []

    try:
        out = subprocess.run(
            ["pgrep", "-f", "gateway serve"],
            capture_output=True, text=True, timeout=2.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return reports
    if out.returncode not in (0, 1):  # 1 = no matches; not a hard error
        return reports

    systemd_pid = _systemd_main_pid()

    for line in out.stdout.splitlines():
        pid_s = line.strip()
        if not pid_s.isdigit():
            continue
        pid = int(pid_s)
        cmdline = _read_cmdline(pid) or ""

        if pid == self_pid:
            reports.append(OrphanReport(pid, cmdline, "skipped:self", None))
            continue
        if not _is_awm_serve_cmdline(cmdline):
            # pgrep matches substring; skip non-`awm gateway serve` matches.
            reports.append(OrphanReport(pid, cmdline, "skipped:not_awm_serve", None))
            continue
        if pid == systemd_pid:
            reports.append(OrphanReport(pid, cmdline, "skipped:in_cgroup", "main pid"))
            continue
        if _is_in_awm_service_cgroup(pid):
            reports.append(OrphanReport(pid, cmdline, "skipped:in_cgroup", None))
            continue

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            reports.append(OrphanReport(pid, cmdline, "killed", "already_gone"))
            continue
        except PermissionError as exc:
            reports.append(OrphanReport(pid, cmdline, "failed", f"perm: {exc}"))
            continue

        # Grace period, then SIGKILL if still alive.
        deadline = time.monotonic() + _KILL_GRACE_S
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                reports.append(OrphanReport(pid, cmdline, "killed", "sigterm"))
                break
            time.sleep(0.1)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
                reports.append(OrphanReport(pid, cmdline, "killed", "sigkill"))
            except ProcessLookupError:
                reports.append(OrphanReport(pid, cmdline, "killed", "sigterm_late"))

    return reports


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
