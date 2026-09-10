"""The link that makes one vault three copies of itself.

Trilium replicates a document between instances over its own sync protocol,
and the topology is a star: an instance has exactly one upstream, so one node
is the hub and every other node is a spoke of it. Both directions travel that
one link, so the result is still a whole copy of everything on every machine.

**Why the link is an ssh forward and not the edge.** The vault child runs with
Trilium's own authentication off (see `server.child_env`), which stands down
the guard on `/api/sync/*` as well — and `/api/sync/stats` carries no guard at
all. So the hub's vault port is a document anyone who can reach it may read and
write, and it must never be reachable off loopback. The awm edge cannot carry
the traffic either: it admits a browser session and deliberately refuses a peer
credential at the vault mount, which is the one credential a machine has. An
ssh port forward reuses authentication each node already has, adds nothing to
any network, and leaves both ends bound to loopback.

**One value decides which node this is.** `TRILIUM_SYNC_HUB` names the ssh
destination of the hub. A node that has it is a spoke: this module holds a
forward open and the vault child is pointed down it. A node without it is the
hub, and is told `disabled` rather than nothing — a spoke's database copied
onto the hub would otherwise bring a spoke's stored `syncServerHost` with it,
and the hub would start syncing with itself through a tunnel it does not have.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from typing import Any

from awm.trilium import instances

log = logging.getLogger("awm.trilium.sync")

#: The loopback port the forward listens on here, unless the environment says
#: otherwise. Owned by this service alone — the tunnel binds it and this node's
#: vault child is pointed at it, and no other process needs to know it, which
#: is why it is not in `awm.config` beside the vault's own port.
DEFAULT_TUNNEL_PORT = 12611

#: Trilium's own value for "a stored sync host is to be ignored". See the
#: module docstring for why the hub is given it explicitly.
DISABLED = "disabled"

#: How long a forward must survive before it counts as healthy. Under it, the
#: retry interval doubles — an unreachable hub is retried at a widening
#: interval rather than every supervision tick for as long as it is down.
SETTLED_S = 60.0
RETRY_MIN_S = float(os.environ.get("TRILIUM_SYNC_RETRY_MIN_S", "20"))
RETRY_MAX_S = float(os.environ.get("TRILIUM_SYNC_RETRY_MAX_S", "300"))


def hub() -> str:
    """The ssh destination of the node holding the vault everyone shares.

    A `Host` alias from the running user's ssh config, so the key, the port and
    the host key live where every other ssh route on this node lives. Empty
    means this node *is* the hub.

    Read on every call rather than frozen at import, because the value arrives
    in the workspace env file and the adapter re-reads that file at start — so
    changing which node is the hub costs a restart of this service rather than
    of the whole gateway.
    """
    return os.environ.get("TRILIUM_SYNC_HUB", "").strip()


def tunnel_port() -> int:
    return int(os.environ.get("TRILIUM_SYNC_TUNNEL_PORT") or DEFAULT_TUNNEL_PORT)


def hub_port() -> int:
    """The vault port on the hub. The same number on every node today, so this
    is the escape hatch for the day one of them differs, not a knob to set."""
    return int(os.environ.get("TRILIUM_SYNC_HUB_PORT") or instances.UPSTREAM_PORT)


def is_client() -> bool:
    """Whether this node syncs to a hub rather than being one."""
    return bool(hub())


def sync_server_host() -> str:
    """What the vault child is told its upstream is.

    Always the local end of the forward, never the hub's address: a node can
    only be pointed at a hub it holds a tunnel to, so there is no way to
    configure a sync target that nothing is listening on.
    """
    if not is_client():
        return DISABLED
    return f"http://127.0.0.1:{tunnel_port()}"


def tunnel_cmd() -> list[str]:
    """The forward, as a command.

    - The listening end is spelled `127.0.0.1` rather than left to ssh's
      default, which `GatewayPorts` in a system config can widen to every
      interface. That would publish an unauthenticated vault.
    - `ExitOnForwardFailure` turns a bound port into an exit the supervision
      loop can see. Without it ssh stays up forwarding nothing, and the vault
      reports a sync host that silently answers nobody.
    - Multiplexing is refused in both directions. A forward carried by somebody
      else's master would outlive a stop of this process and could not be
      signalled by it.
    """
    if not is_client():
        raise RuntimeError("no TRILIUM_SYNC_HUB: this node is the hub")
    local, remote = tunnel_port(), hub_port()
    if local == instances.UPSTREAM_PORT:
        raise RuntimeError(
            f"TRILIUM_SYNC_TUNNEL_PORT is {local}, which is the vault's "
            f"own port — the forward would fight the vault for it")
    return [
        "ssh", "-N", "-T",
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ConnectTimeout=20",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "ControlMaster=no",
        "-o", "ControlPath=none",
        "-L", f"127.0.0.1:{local}:127.0.0.1:{remote}",
        hub(),
    ]


def _preexec() -> None:  # pragma: no cover — runs in the forked child
    """Own session, and die with the parent. Same reasoning as the vault
    child's — see `server._preexec`."""
    os.setsid()
    try:
        import ctypes

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:  # noqa: BLE001 — best effort; the group kill still works
        pass
    if os.getppid() == 1:
        os._exit(1)


class Tunnel:
    """The ssh forward to the hub. Every method is safe to call at any time.

    Inert on a hub: every method returns a state saying so and spawns nothing.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._started_at: float | None = None
        self._last_error: str | None = None
        self._retry_s: float = RETRY_MIN_S
        self._next_attempt: float = 0.0
        self._held = False

    @property
    def log_file(self):
        return instances.LOG_DIR / "tunnel.log"

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def snapshot(self, *, verbose: bool = True) -> dict[str, Any]:
        """Whether this node has a live link to the hub, and to which one."""
        with self._lock:
            proc = self._proc
            state: dict[str, Any] = {
                "role": "client" if is_client() else "hub",
                "hub": hub() or None,
                "running": self._alive(),
                "uptime_s": (round(time.time() - self._started_at, 1)
                             if self._started_at and self._alive() else None),
                "error": self._last_error,
            }
            if is_client():
                state["sync_server_host"] = sync_server_host()
            if verbose:
                state.update({
                    "pid": proc.pid if proc and proc.poll() is None else None,
                    "exit_code": proc.poll() if proc else None,
                    "local_port": tunnel_port() if is_client() else None,
                    "hub_port": hub_port() if is_client() else None,
                    "retry_s": self._retry_s if is_client() else None,
                    "log": str(self.log_file),
                })
            return state

    def _spawn(self) -> None:
        """Open the forward. Caller holds the lock."""
        if shutil.which("ssh") is None:
            raise FileNotFoundError("no ssh on PATH — the sync link needs one")
        cmd = tunnel_cmd()
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        log.info("trilium: opening sync tunnel %s", " ".join(cmd))
        out = open(self.log_file, "ab", buffering=0)  # noqa: SIM115 — owned by the child
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=out,
                stderr=subprocess.STDOUT, preexec_fn=_preexec,
            )
        finally:
            out.close()
        self._started_at = time.time()
        self._last_error = None

    def start(self) -> dict[str, Any]:
        with self._lock:
            self._held = False
            if not is_client():
                return self.snapshot() | {"action": "not-a-client"}
            if self._alive():
                return self.snapshot() | {"action": "already-running"}
            try:
                self._spawn()
            except Exception as exc:  # noqa: BLE001 — reportable, not fatal
                self._last_error = f"{type(exc).__name__}: {exc}"
                raise
            self._next_attempt = time.time() + self._retry_s
        return self.snapshot() | {"action": "started"}

    def stop(self, *, timeout: float = 10.0, hold: bool = False) -> dict[str, Any]:
        """Close the forward. With `hold`, keep the loop from reopening it."""
        with self._lock:
            self._held = hold
            proc = self._proc
            if proc is None or proc.poll() is not None:
                self._proc = None
                return {"action": "not-running", "running": False}
            self._signal_group(proc, signal.SIGTERM)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._signal_group(proc, signal.SIGKILL)
                proc.wait(timeout=5)
            self._proc = None
            self._started_at = None
        return {"action": "stopped", "running": False}

    @staticmethod
    def _signal_group(proc: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                pass

    def reconcile(self) -> dict[str, Any]:
        """Reopen the forward if it has died. Cheap; called on a loop.

        A hub that has been asleep, or an sshd that is not up yet, is a normal
        state rather than an error, so a forward that keeps exiting quickly is
        retried at a widening interval instead of every tick. One that survives
        `SETTLED_S` puts the interval back to its floor.
        """
        with self._lock:
            if not is_client():
                return {"action": "not-a-client"}
            if self._alive():
                if (self._started_at
                        and time.time() - self._started_at >= SETTLED_S
                        and self._retry_s != RETRY_MIN_S):
                    self._retry_s = RETRY_MIN_S
                return {"action": "none"}
            if self._held:
                return {"action": "held"}
            lived = (time.time() - self._started_at) if self._started_at else None
            if lived is not None and lived < SETTLED_S:
                self._retry_s = min(self._retry_s * 2, RETRY_MAX_S)
            now = time.time()
            if now < self._next_attempt:
                return {"action": "waiting",
                        "in_s": round(self._next_attempt - now, 1)}
            code = self._proc.poll() if self._proc else None
            try:
                self._spawn()
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._next_attempt = now + self._retry_s
                return {"action": "reopen-failed", "error": self._last_error}
            self._next_attempt = time.time() + self._retry_s
        return {"action": "reopened", "previous_exit": code,
                "retry_s": self._retry_s}

    def logs(self, tail: int = 200) -> str:
        try:
            with open(self.log_file, "rb") as fh:
                return b"".join(fh.readlines()[-tail:]).decode("utf-8", "replace")
        except OSError as exc:
            return f"(no log at {self.log_file}: {exc})"


#: The one link this node holds. Inert on the hub.
TUNNEL = Tunnel()
