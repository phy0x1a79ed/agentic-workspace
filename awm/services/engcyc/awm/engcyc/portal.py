"""Supervision of the EngCyc portal's gunicorn server.

The portal is a Flask app held as its own awm project. Its ``release`` worktree
is the code this service runs. A reverse proxy on another machine fronts it, so
the bind address comes from ``ENGCYC_BIND`` and this module never chooses one
wider than it is given.

The child gets only the ``ENGCYC_*`` variables and a ``PATH``: it is the one
process in the service that faces the internet, and nothing else in the awm
environment is its business.

Gunicorn's master outlives its workers, so process liveness drives respawn while
``status`` also probes the app over HTTP. Those are two different failures and
are reported apart.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from awm.config import AWM_DIR, WORKSPACE_ROOT

log = logging.getLogger("awm.engcyc.portal")

STATE_DIR = Path(os.environ.get("ENGCYC_STATE") or (AWM_DIR / "services" / "engcyc"))
PORTAL_DIR = Path(os.environ.get("ENGCYC_PORTAL_DIR")
                  or (WORKSPACE_ROOT / "projects" / "engcyc-portal" / "release"))
WSGI_FILE = PORTAL_DIR / "src" / "server" / "wsgi.py"
GUNICORN_BIN_FILE = STATE_DIR / "gunicorn-bin"
LOG_FILE = STATE_DIR / "engcyc.log"
BIND = os.environ.get("ENGCYC_BIND", "127.0.0.1:12001")
PROBE_PATH = "/api/v1/get_fields"

HEALTH_INTERVAL_S = float(os.environ.get("ENGCYC_HEALTH_INTERVAL_S", "20"))
START_TIMEOUT_S = float(os.environ.get("ENGCYC_START_TIMEOUT_S", "60"))
CHILD_PATH = "/usr/bin:/bin"


def gunicorn_exe() -> str | None:
    try:
        exe = GUNICORN_BIN_FILE.read_text().strip()
    except OSError:
        return None
    return exe if exe and os.access(exe, os.X_OK) else None


def installed() -> str | None:
    """Why the portal cannot run here, or None when it can."""
    if not WSGI_FILE.is_file():
        return f"no portal checkout at {PORTAL_DIR}"
    if gunicorn_exe() is None:
        return f"no gunicorn recorded in {GUNICORN_BIN_FILE}; run install.sh"
    return None


def probe(timeout: float = 5.0) -> dict[str, Any]:
    """GET the field list the portal's frontend loads first."""
    url = f"http://{BIND}{PROBE_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            fields = json.load(resp)
            return {"url": url, "status": resp.status, "fields": len(fields)}
    except Exception as exc:  # noqa: BLE001 — every failure is a status answer
        return {"url": url, "error": f"{type(exc).__name__}: {exc}"}


def child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.startswith("ENGCYC_")}
    env["ENGCYC_STATE"] = str(STATE_DIR)
    env["ENGCYC_BIND"] = BIND
    exe = gunicorn_exe()
    env["PATH"] = (str(Path(exe).parent) + os.pathsep if exe else "") + CHILD_PATH
    return env


def _preexec() -> None:  # pragma: no cover — runs in the forked child
    """Own session (so the tree is one signalable group) + die with the parent."""
    os.setsid()
    try:
        import ctypes

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:  # noqa: BLE001 — best effort; the group kill still works
        pass
    if os.getppid() == 1:
        os._exit(1)


class Supervisor:
    """Owns exactly one gunicorn master. Every method is safe to call at any time."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._started_at: float | None = None
        self._last_error: str | None = None
        self._stopped = False

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            proc = self._proc
            alive = self._alive()
            snap = {
                "unavailable": installed(),
                "running": alive,
                "pid": proc.pid if alive else None,
                "exit_code": proc.poll() if proc else None,
                "bind": BIND,
                "uptime_s": (round(time.time() - self._started_at, 1)
                             if self._started_at and alive else None),
                "portal": str(PORTAL_DIR),
                "state": str(STATE_DIR),
                "gunicorn": gunicorn_exe(),
                "error": self._last_error,
            }
        snap["probe"] = probe() if alive else None
        return snap

    def _spawn(self) -> None:
        """Launch gunicorn. Caller holds the lock."""
        why = installed()
        if why:
            raise FileNotFoundError(why)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        cmd = [gunicorn_exe(), "-c", "server/gunicorn.conf.py", "server.wsgi"]
        log.info("engcyc: launching %s on %s", " ".join(cmd), BIND)
        # Append: the log is the only account of a crash between two `status` calls.
        out = open(LOG_FILE, "ab", buffering=0)  # noqa: SIM115 — owned by the child
        try:
            self._proc = subprocess.Popen(
                cmd, cwd=str(PORTAL_DIR / "src"), env=child_env(),
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                preexec_fn=_preexec,
            )
        finally:
            out.close()
        self._started_at = time.time()
        self._last_error = None

    def start(self, *, wait: bool = True) -> dict[str, Any]:
        with self._lock:
            self._stopped = False
            if self._alive():
                return self.snapshot() | {"action": "already-running"}
            try:
                self._spawn()
            except Exception as exc:  # noqa: BLE001 — reportable, not fatal
                self._last_error = f"{type(exc).__name__}: {exc}"
                raise
        if wait:
            self._await_ready()
        return self.snapshot() | {"action": "started"}

    def _await_ready(self) -> None:
        deadline = time.time() + START_TIMEOUT_S
        while time.time() < deadline:
            if "status" in probe(timeout=2):
                return
            if not self._alive():
                self._last_error = (
                    f"gunicorn exited with {self._proc.poll() if self._proc else '?'} "
                    f"before serving; see {LOG_FILE}")
                return
            time.sleep(0.5)
        self._last_error = f"the portal did not answer on {BIND} within {START_TIMEOUT_S}s"

    def stop(self, *, timeout: float = 30.0) -> dict[str, Any]:
        with self._lock:
            self._stopped = True
            proc = self._proc
            if proc is None or proc.poll() is not None:
                self._proc = None
                return {"action": "not-running", "running": False}
            # SIGTERM to the group lets gunicorn finish in-flight requests.
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

    def restart(self) -> dict[str, Any]:
        self.stop()
        return self.start() | {"action": "restarted"}

    def reconcile(self) -> dict[str, Any]:
        """Respawn gunicorn if it has died and nobody stopped it. Called on a loop."""
        with self._lock:
            if self._stopped or self._alive() or installed():
                return {"action": "none"}
            code = self._proc.poll() if self._proc else None
            try:
                self._spawn()
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"{type(exc).__name__}: {exc}"
                return {"action": "respawn-failed", "error": self._last_error}
        return {"action": "respawned", "previous_exit": code}

    def logs(self, tail: int = 200) -> str:
        try:
            with open(LOG_FILE, "rb") as fh:
                return b"".join(fh.readlines()[-tail:]).decode("utf-8", "replace")
        except OSError as exc:
            return f"(no log at {LOG_FILE}: {exc})"
