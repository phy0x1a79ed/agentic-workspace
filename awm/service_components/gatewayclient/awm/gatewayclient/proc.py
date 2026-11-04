"""How a service parents a child process, signals its group, or hands one to the user's systemd manager.

A library because the plugins that spawn children may not import each other
(install.md R117 item 1).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from pathlib import Path

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
