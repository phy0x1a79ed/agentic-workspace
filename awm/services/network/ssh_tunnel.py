"""SSH-tunneled peer transport.

Each registered peer gets a lazy, ControlMaster-pooled SSH tunnel that
port-forwards a local 127.0.0.1 port to the peer's localhost-bound awm
listener. The tunnel is the substrate for all federation HTTP and
WebSocket traffic — `acquire_tunnel(peer_id)` returns a Tunnel whose
`.local_url` is the http://127.0.0.1:<port> federation can hit.

Design:
- One ControlMaster per peer, socket at AWM_DIR/ssh/peer-<peer_id>.sock.
- Lazy: the master is opened on first acquire and persists for
  CONTROL_PERSIST (10m) of idle, then ssh tears it down. The cached
  Tunnel is reopened transparently on first re-use.
- Port allocation: bind 127.0.0.1:0, grab the ephemeral port, close
  the probe, hand it to `ssh -L`. Race-tolerant in practice.
- Health: `ssh -O check` for the master, plus a quick GET /peer.
- Reopen: up to 2 attempts before raising TunnelError.
"""

from __future__ import annotations

import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from awm import config
from awm.services.network import peers as peer_svc

SSH_DIR = config.AWM_DIR / "ssh"
CONTROL_PERSIST = "10m"
_OPEN_TIMEOUT = 15.0
_CHECK_TIMEOUT = 5.0
_PROBE_TIMEOUT = 3.0


class TunnelError(Exception):
    """Tunnel could not be established or maintained."""


@dataclass
class Tunnel:
    peer_id: str
    local_port: int
    sock_path: str
    opened_at: float
    remote_port: int
    ssh_alias: str

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.local_port}"


_lock = threading.Lock()
_tunnels: dict[str, Tunnel] = {}


def _pick_free_port() -> int:
    """Bind to an ephemeral port and immediately release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _master_alive(sock_path: str, ssh_alias: str) -> bool:
    if not Path(sock_path).exists():
        return False
    try:
        r = subprocess.run(
            ["ssh", "-O", "check", "-o", f"ControlPath={sock_path}", ssh_alias],
            capture_output=True, timeout=_CHECK_TIMEOUT,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _probe(local_port: int) -> bool:
    """Any HTTP response on /peer means the port-forward is live."""
    try:
        r = httpx.get(
            f"http://127.0.0.1:{local_port}/peer",
            timeout=_PROBE_TIMEOUT,
            follow_redirects=False,
        )
        return r.status_code < 500 or r.status_code == 503
    except httpx.HTTPError:
        return False


def _open_master(ssh_alias: str, remote_port: int, sock_path: str) -> int:
    SSH_DIR.mkdir(parents=True, exist_ok=True)
    local_port = _pick_free_port()
    cmd = [
        "ssh",
        "-fN",
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={sock_path}",
        "-o", f"ControlPersist={CONTROL_PERSIST}",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-L", f"{local_port}:127.0.0.1:{remote_port}",
        ssh_alias,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=_OPEN_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise TunnelError(f"ssh master open timed out for {ssh_alias}") from exc
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", errors="replace").strip()
        raise TunnelError(f"ssh master open failed for {ssh_alias}: {err[:300]}")
    return local_port


def _close(peer_id: str, sock_path: str, ssh_alias: str) -> None:
    try:
        subprocess.run(
            ["ssh", "-O", "exit", "-o", f"ControlPath={sock_path}", ssh_alias],
            capture_output=True, timeout=_CHECK_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass
    _tunnels.pop(peer_id, None)


def acquire_tunnel(peer_id: str) -> Tunnel:
    """Return a live Tunnel for peer_id, opening or reopening as needed.

    Two reopen attempts before raising TunnelError. Safe to call from any
    thread (single coarse lock — tunnel acquisition is the bottleneck,
    not the throughput).

    If the peer's ``ssh_alias`` is empty, we assume the remote awm is
    already reachable at ``127.0.0.1:remote_port`` (typically because an
    out-of-band reverse SSH forward is maintained by the *other* peer).
    In that mode no SSH process is spawned by this peer.
    """
    peer = peer_svc.get_peer(peer_id)
    if peer is None:
        raise TunnelError(f"unknown peer: {peer_id}")
    ssh_alias = peer["ssh_alias"] or ""
    remote_port = peer["remote_port"]
    sock_path = str(SSH_DIR / f"peer-{peer_id}.sock")

    if not ssh_alias:
        # Loopback mode — relies on an existing reverse-forward.
        if not _probe(remote_port):
            raise TunnelError(
                f"peer {peer_id} in loopback mode but 127.0.0.1:{remote_port} "
                f"is not reachable (reverse-forward down?)"
            )
        return Tunnel(
            peer_id=peer_id, local_port=remote_port, sock_path="",
            opened_at=time.time(), remote_port=remote_port, ssh_alias="",
        )

    with _lock:
        cached = _tunnels.get(peer_id)
        if cached and _master_alive(sock_path, ssh_alias) and _probe(cached.local_port):
            return cached

        if cached:
            _close(peer_id, sock_path, ssh_alias)

        last_err: str | None = None
        for _ in range(2):
            try:
                local_port = _open_master(ssh_alias, remote_port, sock_path)
            except TunnelError as exc:
                last_err = str(exc)
                continue

            # Master started, but the forward may need a beat to be ready.
            for _ in range(10):
                if _probe(local_port):
                    tun = Tunnel(
                        peer_id=peer_id,
                        local_port=local_port,
                        sock_path=sock_path,
                        opened_at=time.time(),
                        remote_port=remote_port,
                        ssh_alias=ssh_alias,
                    )
                    _tunnels[peer_id] = tun
                    return tun
                time.sleep(0.2)

            last_err = "health probe failed after master open"
            _close(peer_id, sock_path, ssh_alias)

        raise TunnelError(
            f"could not open tunnel to {peer_id} (ssh {ssh_alias}): {last_err}"
        )


def release_tunnel(peer_id: str) -> None:
    """Close one peer's tunnel. No-op if not open."""
    with _lock:
        tun = _tunnels.get(peer_id)
        if tun is None:
            return
        _close(peer_id, tun.sock_path, tun.ssh_alias)


def release_all_tunnels() -> None:
    """Tear down every tunnel. Called on shutdown."""
    with _lock:
        for pid, tun in list(_tunnels.items()):
            _close(pid, tun.sock_path, tun.ssh_alias)


def list_open_tunnels() -> list[dict]:
    """For diagnostics."""
    with _lock:
        return [
            {
                "peer_id": t.peer_id,
                "local_port": t.local_port,
                "remote_port": t.remote_port,
                "ssh_alias": t.ssh_alias,
                "sock_path": t.sock_path,
                "opened_at": t.opened_at,
            }
            for t in _tunnels.values()
        ]
