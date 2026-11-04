"""A supervised child in its own process group, which dies with its parent."""

from __future__ import annotations

import os
import signal
import subprocess
from typing import Any


def preexec() -> None:  # pragma: no cover — runs in the forked child
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


def signal_group(proc: subprocess.Popen, sig: int) -> None:
    """Signal the whole group. The pid alone leaves anything the child spawned holding its port."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass


class GroupChild:
    """The stop half of a supervised child: a subclass sets ``_lock``, ``_proc``,
    ``_held`` and ``_started_at`` and spawns with :func:`preexec`."""

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
