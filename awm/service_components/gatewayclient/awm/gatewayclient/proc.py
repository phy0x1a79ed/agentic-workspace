"""How a service parents a child process, signals and stops its group, or hands one to the user's systemd manager.

A library because the plugins that spawn children may not import each other
(install.md R117 item 1).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any

PR_SET_PDEATHSIG = 1


def child_preexec() -> None:  # pragma: no cover — runs in the forked child
    """``preexec_fn`` for a child that dies with its parent: own session, death signal, re-check.

    Order matters: ``setsid`` first so the tree is one signalable group, then
    the death signal, then a re-check. A parent that exited between the fork
    and ``prctl`` would leave a child nothing is ever going to signal.
    """
    os.setsid()
    try:
        import ctypes

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    except Exception:  # noqa: BLE001 — best effort; the group kill still works
        pass
    if os.getppid() == 1:
        os._exit(1)


def user_manager_env() -> dict[str, str] | None:
    """Env for ``systemd-run --user`` to reach this user's manager, or None when there is none.

    A transient unit of the user manager lives in a cgroup that awm.service
    does not own, so a ``systemctl restart awm`` does not kill it.
    """
    if not shutil.which("systemd-run"):
        return None
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    if not Path(runtime, "bus").is_socket():
        return None
    return {
        "XDG_RUNTIME_DIR": runtime,
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus",
    }


def signal_group(proc: subprocess.Popen, sig: int) -> None:
    """Signal the child's whole process group, or the child alone when the group is gone.

    The group, not the pid: a child may have spawned children of its own, and
    signalling one of them leaves the rest holding the socket.
    """
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass


class GroupChild:
    """The stop half of a supervised child: a subclass sets ``_lock``, ``_proc``,
    ``_held`` and ``_started_at`` and spawns the child in its own session."""

    STOP_TIMEOUT_S = 10.0

    def stop(self, *, timeout: float | None = None, hold: bool = False) -> dict[str, Any]:
        """Stop the child. With `hold`, keep the loop from bringing it back.

        A hold is released by the next `start`, so the pairing is stop-then-start
        rather than a lock somebody has to remember to drop. Whatever holds one
        must start the child again even when its own work failed, or everyone is
        left with no server and no error that says why.
        """
        with self._lock:
            self._held = hold
            proc = self._proc
            if proc is None or proc.poll() is not None:
                self._proc = None
                return {"action": "not-running", "running": False}
            signal_group(proc, signal.SIGTERM)
            try:
                proc.wait(timeout=timeout or self.STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                signal_group(proc, signal.SIGKILL)
                proc.wait(timeout=5)
            self._proc = None
            self._started_at = None
        return {"action": "stopped", "running": False}
