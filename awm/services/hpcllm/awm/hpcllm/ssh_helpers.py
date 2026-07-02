from __future__ import annotations

import asyncio
import logging
import subprocess

from awm import gatewayclient

log = logging.getLogger("awm.hpcllm.ssh")


def ensure_connected(host: str) -> dict:
    """Call svc-ssh to establish a ControlMaster connection to host.

    Blocks up to 120s (svc-ssh timeout) for cold VPN + Duo. Returns the
    status dict from svc-ssh. Raises on failure.
    """
    try:
        result = gatewayclient.call_sync("ssh", "connect", {"host": host},
                                         timeout=130)
    except gatewayclient.GatewayCallError as e:
        log.error("svc-ssh connect to %s failed: %s", host, e)
        raise RuntimeError(f"ssh connect to {host} failed: {e}") from e

    if isinstance(result, dict) and result.get("status") == "error":
        err = result.get("error", "unknown error")
        log.error("svc-ssh connect to %s returned error: %s", host, err)
        raise RuntimeError(f"ssh connect to {host} returned error: {err}")

    log.info("ControlMaster to %s established", host)
    return result or {}


def ssh_run(host: str, cmd: str, *,
            timeout: float = 60.0,
            check: bool = True) -> subprocess.CompletedProcess:
    """Run a command on host via SSH (reuses ControlMaster)."""
    r = subprocess.run(
        ["ssh", host, cmd],
        capture_output=True, text=True,
        timeout=timeout,
    )
    if check and r.returncode != 0:
        log.error("ssh %s cmd failed (rc=%d): %s", host, r.returncode,
                  r.stderr.strip() or r.stdout.strip())
        raise RuntimeError(
            f"ssh {host} cmd exited {r.returncode}: "
            f"{r.stderr.strip() or r.stdout.strip()}"
        )
    return r


async def ssh_run_async(host: str, cmd: str, *,
                        timeout: float = 60.0) -> tuple[int, str, str]:
    """Async variant of ssh_run. Returns (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "ssh", host, cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"ssh {host} cmd timed out after {timeout}s")

    out = stdout.decode().strip()
    err = stderr.decode().strip()
    return proc.returncode or 0, out, err


def ssh_forward(host: str, local_port: int, remote_host: str,
                remote_port: int) -> None:
    """Create an SSH tunnel via the ControlMaster connection.

    localhost:local_port -> remote_host:remote_port through host.
    """
    r = subprocess.run(
        ["ssh", "-O", "forward",
         f"-L{local_port}:{remote_host}:{remote_port}",
         host],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        log.error("ssh forward to %s failed: %s", host, r.stderr.strip())
        raise RuntimeError(
            f"ssh forward to {host} failed: {r.stderr.strip()}"
        )
    log.info("tunnel active: localhost:%d -> %s:%d via %s",
             local_port, remote_host, remote_port, host)


def ssh_cancel_forward(host: str, local_port: int, remote_host: str,
                       remote_port: int) -> None:
    """Remove an SSH tunnel from the ControlMaster connection."""
    r = subprocess.run(
        ["ssh", "-O", "cancel",
         f"-L{local_port}:{remote_host}:{remote_port}",
         host],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        log.warning("ssh cancel forward to %s warning: %s",
                    host, r.stderr.strip())


def ssh_write_file(host: str, path: str, content: str) -> None:
    """Write content to a remote file via SSH stdin pipe."""
    proc = subprocess.Popen(
        ["ssh", host, f"cat > {path}"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = proc.communicate(input=content.encode(), timeout=30.0)
    if proc.returncode != 0:
        log.error("ssh write file to %s:%s failed: %s",
                  host, path, stderr.decode().strip())
        raise RuntimeError(
            f"ssh write file to {host}:{path} failed: "
            f"{stderr.decode().strip()}"
        )


def ssh_read_file(host: str, path: str) -> str | None:
    """Read a remote file. Returns None if file doesn't exist."""
    r = subprocess.run(
        ["ssh", host, f"cat {path} 2>/dev/null || true"],
        capture_output=True, text=True,
        timeout=15.0,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return r.stdout.strip()


async def ssh_read_file_async(host: str, path: str) -> str | None:
    """Async variant of ssh_read_file."""
    return await asyncio.to_thread(ssh_read_file, host, path)


def ssh_rm(host: str, path: str) -> None:
    """Remove a remote file or directory recursively."""
    subprocess.run(
        ["ssh", host, f"rm -rf {path}"],
        capture_output=True, text=True,
    )


async def ssh_rm_async(host: str, path: str) -> None:
    """Async variant of ssh_rm."""
    return await asyncio.to_thread(ssh_rm, host, path)
