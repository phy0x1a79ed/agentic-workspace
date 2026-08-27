"""Supervision of one Trilium node process per user.

Each child binds plain HTTP on loopback with no transport security of its own.
That is the right default and we keep it: the mesh-facing half is a separate
TLS front per user (see `front`), and nothing here widens the bind. Trilium's
own login still runs on every child, which is what makes two people on one node
two different people.

**The children are parented.** Trilium persists everything to its data
directory, so an awm restart costs a browser reload rather than any content.
Parenthood is what makes one supervised lifetime cover the whole stack — no
adoption protocol, and no orphan to find later. Each child is put in its own
session so its whole process tree can be signalled as a group, and
`PR_SET_PDEATHSIG` closes the window where this process dies between the fork
and the child's first instruction.

**Where things live.** A user's content is in their `userdata` scope worktree,
never in service state — see `instances`. Service state holds only the port
allocation, the recorded node bin, the tarball fallback, and these logs.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from awm.trilium import instances
from awm.trilium.instances import Instance

log = logging.getLogger("awm.trilium.server")


def _preexec() -> None:  # pragma: no cover — runs in the forked child
    """Own session (so the tree is one signalable group) + die with the parent.

    Order matters: `setsid` first, then the death signal, then a re-check that
    the parent is still alive — a parent that exited between the fork and
    `prctl` would leave a child nothing will ever signal.
    """
    os.setsid()
    try:
        import ctypes

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:  # noqa: BLE001 — best effort; the group kill still works
        pass
    if os.getppid() == 1:
        os._exit(1)


def child_env(inst: Instance) -> dict[str, str]:
    """The environment one child runs under.

    Every value here is a decision, so they are named rather than templated:

    - The data directory is the user's own scope worktree, which is what makes
      one person's content one branch.
    - The backup directory is split out of it deliberately. It is the only
      database file safe to pin, because Trilium writes those under its sync
      mutex while `document.db` and its write-ahead log are live.
    - The bind is loopback. The front is the only way in.
    - `trustedReverseProxy` is required, not cosmetic: httpsfront terminates TLS
      and forwards `X-Forwarded-Proto: https`. Without the trust setting express
      reports the request as plain HTTP, and Trilium then declines to set its
      session cookie `Secure`. `loopback` rather than `true` because the front
      always connects from 127.0.0.1 and a blanket trust would let a forged
      `X-Forwarded-For` past anything that reads a client address.
    - Backend scripting and the SQL console are both off by default on a server
      build. They are set anyway: a `config.ini` in the data directory can turn
      either on, and on a public host both are arbitrary code execution.
    """
    env = dict(os.environ)
    env.update({
        "TRILIUM_ENV": "production",
        "TRILIUM_DATA_DIR": str(inst.data_dir),
        "TRILIUM_BACKUP_DIR": str(inst.backup_dir),
        "TRILIUM_HOST": "127.0.0.1",
        "TRILIUM_PORT": str(inst.upstream_port),
        "TRILIUM_NETWORK_TRUSTEDREVERSEPROXY": "loopback",
        "TRILIUM_SECURITY_BACKEND_SCRIPTING_ENABLED": "false",
        "TRILIUM_SECURITY_SQL_CONSOLE_ENABLED": "false",
        "HOME": str(Path.home()),
    })
    nb = instances.NODE_BIN_FILE
    try:
        recorded = nb.read_text().strip()
    except OSError:
        recorded = ""
    if recorded:
        env["PATH"] = recorded + os.pathsep + env.get("PATH", "")
    return env


class Child:
    """One user's node process. Every method is safe to call at any time."""

    def __init__(self, inst: Instance) -> None:
        self.inst = inst
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._started_at: float | None = None
        self._last_error: str | None = None

    # -- state --------------------------------------------------------------

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            proc = self._proc
            inst = self.inst
            return {
                "user": inst.user,
                "running": self._alive(),
                "pid": proc.pid if proc and proc.poll() is None else None,
                "exit_code": proc.poll() if proc else None,
                "port": inst.upstream_port,
                "listening": instances.listening(inst.upstream_port),
                "uptime_s": (round(time.time() - self._started_at, 1)
                             if self._started_at and self._alive() else None),
                "scope": str(inst.scope),
                "data_dir": str(inst.data_dir),
                "backups": self.backups(),
                "log": str(inst.log_file),
                "error": self._last_error,
            }

    def backups(self) -> list[str]:
        """The rolling database copies present, newest name order.

        Reported per user because "the service is up" and "this person has a
        recoverable copy" are different facts, and only the second one matters
        the day someone needs it.
        """
        try:
            return sorted(p.name for p in self.inst.backup_dir.glob("backup-*.db"))
        except OSError:
            return []

    # -- lifecycle ----------------------------------------------------------

    def _spawn(self) -> None:
        """Launch this user's server. Caller holds the lock."""
        chosen = instances.entry_point()
        if chosen is None:
            raise FileNotFoundError(
                f"no Trilium server bundle: neither {instances.FORK_ENTRY} nor "
                f"{instances.TARBALL_ENTRY} exists — run "
                f"awm/services/trilium/install.sh")
        entry, _source = chosen

        inst = self.inst
        inst.data_dir.mkdir(parents=True, exist_ok=True)
        inst.backup_dir.mkdir(parents=True, exist_ok=True)
        inst.log_file.parent.mkdir(parents=True, exist_ok=True)

        # An absolute path to the bundle, and the cwd is irrelevant: in
        # production Trilium derives its resource directory from
        # `path.dirname(process.argv[1])`, so the assets are found relative to
        # the bundle rather than to wherever the supervisor happened to be.
        cmd = [instances.node_exe(), str(entry)]
        log.info("trilium[%s]: launching %s (port %d, data %s)",
                 inst.user, " ".join(cmd), inst.upstream_port, inst.data_dir)
        # Append rather than truncate: the log is the only account of a crash
        # that happened between two `status` calls.
        out = open(inst.log_file, "ab", buffering=0)  # noqa: SIM115 — owned by the child
        try:
            self._proc = subprocess.Popen(
                cmd, cwd=str(entry.parent), env=child_env(inst),
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                preexec_fn=_preexec,
            )
        finally:
            out.close()
        self._started_at = time.time()
        self._last_error = None

    def start(self, *, wait: bool = True) -> dict[str, Any]:
        with self._lock:
            if self._alive():
                return self.snapshot() | {"action": "already-running"}
            try:
                self._spawn()
            except Exception as exc:  # noqa: BLE001 — reportable, not fatal
                self._last_error = f"{type(exc).__name__}: {exc}"
                raise
        if wait:
            self._await_listening()
        return self.snapshot() | {"action": "started"}

    def _await_listening(self) -> None:
        deadline = time.time() + instances.START_TIMEOUT_S
        port = self.inst.upstream_port
        while time.time() < deadline:
            if instances.listening(port):
                return
            if not self._alive():
                self._last_error = (
                    f"exited with {self._proc.poll() if self._proc else '?'} "
                    f"before binding {port}; see {self.inst.log_file}")
                return
            time.sleep(0.4)
        self._last_error = (f"did not bind {port} within "
                            f"{instances.START_TIMEOUT_S}s")

    def stop(self, *, timeout: float = 20.0) -> dict[str, Any]:
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                self._proc = None
                return {"user": self.inst.user, "action": "not-running",
                        "running": False}
            # The whole group. Signalling the pid alone leaves anything node
            # spawned holding the port.
            self._signal_group(proc, signal.SIGTERM)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._signal_group(proc, signal.SIGKILL)
                proc.wait(timeout=5)
            self._proc = None
            self._started_at = None
        return {"user": self.inst.user, "action": "stopped", "running": False}

    @staticmethod
    def _signal_group(proc: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                pass

    def restart(self) -> dict[str, Any]:
        self.stop()
        return self.start() | {"action": "restarted"}

    def reconcile(self) -> dict[str, Any]:
        """Respawn if this child has actually died. Cheap; called on a loop."""
        with self._lock:
            if self._alive():
                return {"user": self.inst.user, "action": "none"}
            code = self._proc.poll() if self._proc else None
            try:
                self._spawn()
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"{type(exc).__name__}: {exc}"
                return {"user": self.inst.user, "action": "respawn-failed",
                        "error": self._last_error}
        return {"user": self.inst.user, "action": "respawned",
                "previous_exit": code}

    def logs(self, tail: int = 200) -> str:
        try:
            with open(self.inst.log_file, "rb") as fh:
                return b"".join(fh.readlines()[-tail:]).decode("utf-8", "replace")
        except OSError as exc:
            return f"(no log at {self.inst.log_file}: {exc})"


class Fleet:
    """Every user's child, kept in step with the scopes on disk.

    A user added while the service runs gets a child on the next reconcile
    pass, and one whose scope was deleted has their child stopped. That is why
    discovery is re-read on every pass rather than captured at startup: adding
    a person is `awm scope create` and nothing else.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._children: dict[str, Child] = {}

    def children(self) -> list[Child]:
        with self._lock:
            return list(self._children.values())

    def get(self, user: str) -> Child | None:
        with self._lock:
            return self._children.get(user)

    def sync(self) -> dict[str, Any]:
        """Create children for new users, retire children for departed ones."""
        added, removed = [], []
        with self._lock:
            live = {i.user: i for i in instances.instances()}
            for user, inst in live.items():
                if user not in self._children:
                    self._children[user] = Child(inst)
                    added.append(user)
            for user in list(self._children):
                if user not in live:
                    self._children.pop(user).stop()
                    removed.append(user)
        return {"added": added, "removed": removed,
                "users": sorted(live)}

    def start_all(self) -> list[dict[str, Any]]:
        self.sync()
        out = []
        for child in self.children():
            try:
                out.append(child.start())
            except Exception as exc:  # noqa: BLE001 — one bad user is not all of them
                log.exception("trilium[%s]: start failed", child.inst.user)
                out.append({"user": child.inst.user, "action": "start-failed",
                            "error": f"{type(exc).__name__}: {exc}"})
        return out

    def stop_all(self) -> list[dict[str, Any]]:
        return [child.stop() for child in self.children()]

    def reconcile(self) -> list[dict[str, Any]]:
        self.sync()
        return [child.reconcile() for child in self.children()
                if not child._alive()]

    def snapshot(self) -> list[dict[str, Any]]:
        return [child.snapshot() for child in self.children()]
