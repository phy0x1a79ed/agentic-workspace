"""Supervision of the one kb server process.

The child is `python -m kb.server` from the kb checkout, in the `kb` env. It
starts its own ingest worker and keeps it alive. This module keeps the server
alive.

The child is parented: its own session, so the whole tree (server and worker)
is signalled as one group, and `PR_SET_PDEATHSIG`, so it dies with this
process. The store is on disk, so a restart costs a warm-up and no data.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
from typing import Any

from awm.kb import instances

log = logging.getLogger("awm.kb.server")


def _preexec() -> None:  # pragma: no cover — runs in the forked child
    os.setsid()
    try:
        import ctypes

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:  # noqa: BLE001 — best effort; the group kill still works
        pass
    if os.getppid() == 1:
        os._exit(1)


def openrouter_key() -> str | None:
    """The OpenRouter key from opencode's auth store. Never logged, only handed to the child."""
    try:
        entry = json.loads(instances.AUTH_JSON.read_text()).get("openrouter") or {}
    except (OSError, ValueError):
        return None
    return (entry.get("key") or "").strip() or None


def child_env() -> dict[str, str]:
    """The child's environment.

    `PYTHONPATH` is replaced, not extended: the awm source a dev sandbox puts
    there must never reach the kb interpreter. The LLM key serves only the
    `answer` and `graph` recall modes. Ingest defaults to chunk-only and needs none.
    """
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "DEV_PYTHONPATH")}
    env.update({
        "PYTHONPATH": str(instances.CHECKOUT / "src"),
        "KB_LIVE": str(instances.LIVE),
        "KB_HOST": instances.HOST,
        "KB_PORT": str(instances.PORT),
    })
    if "KB_ZOTERO_LIBRARY" not in env and instances.ZOTERO_LIBRARY.is_file():
        env["KB_ZOTERO_LIBRARY"] = str(instances.ZOTERO_LIBRARY)
    if not env.get("LLM_API_KEY"):
        key = openrouter_key()
        if key:
            env["LLM_API_KEY"] = key
    return env


class Child:
    """The kb server process. Every method is safe to call at any time."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._started_at: float | None = None
        self._last_error: str | None = None
        self._held = False

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            proc = self._proc
            return {
                "running": self._alive(),
                "listening": instances.listening(),
                "held": self._held,
                "pid": proc.pid if proc and proc.poll() is None else None,
                "exit_code": proc.poll() if proc else None,
                "uptime_s": (round(time.time() - self._started_at, 1)
                             if self._started_at and self._alive() else None),
                "error": self._last_error,
                "url": instances.URL,
                "checkout": str(instances.CHECKOUT),
                "live": str(instances.LIVE),
                "log": str(instances.LOG_FILE),
            }

    def _spawn(self) -> None:
        """Launch the server. Caller holds the lock."""
        if not instances.has_server():
            raise FileNotFoundError(f"no kb server in {instances.CHECKOUT} — set KB_CHECKOUT")
        py = instances.python()
        if py is None:
            raise FileNotFoundError("no kb env interpreter recorded — run awm/services/kb/install.sh")
        instances.LIVE.mkdir(parents=True, exist_ok=True)
        instances.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        cmd = [py, "-m", "kb.server"]
        log.info("kb: launching %s (port %d, live %s)", " ".join(cmd), instances.PORT, instances.LIVE)
        out = open(instances.LOG_FILE, "ab", buffering=0)  # noqa: SIM115 — owned by the child
        try:
            self._proc = subprocess.Popen(
                cmd, cwd=str(instances.CHECKOUT), env=child_env(),
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                preexec_fn=_preexec,
            )
        finally:
            out.close()
        self._started_at = time.time()
        self._last_error = None

    def start(self, *, wait: bool = True) -> dict[str, Any]:
        with self._lock:
            self._held = False
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
        while time.time() < deadline:
            if instances.listening():
                return
            if not self._alive():
                self._last_error = (f"exited with {self._proc.poll() if self._proc else '?'} "
                                    f"before binding {instances.PORT}; see {instances.LOG_FILE}")
                return
            time.sleep(0.5)
        self._last_error = f"did not bind {instances.PORT} within {instances.START_TIMEOUT_S}s"

    def stop(self, *, timeout: float = 30.0, hold: bool = False) -> dict[str, Any]:
        """Stop the server and its worker. With `hold`, keep `reconcile` from respawning it until the next `start`."""
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

    def restart(self) -> dict[str, Any]:
        self.stop()
        return self.start() | {"action": "restarted"}

    def reconcile(self) -> dict[str, Any]:
        """Respawn the server if it died. Cheap; called on a loop."""
        with self._lock:
            if self._alive():
                return {"action": "none"}
            if self._held:
                return {"action": "held"}
            code = self._proc.poll() if self._proc else None
            try:
                self._spawn()
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"{type(exc).__name__}: {exc}"
                return {"action": "respawn-failed", "error": self._last_error}
        return {"action": "respawned", "previous_exit": code}

    def logs(self, tail: int = 200) -> str:
        try:
            with open(instances.LOG_FILE, "rb") as fh:
                return b"".join(fh.readlines()[-tail:]).decode("utf-8", "replace")
        except OSError as exc:
            return f"(no log at {instances.LOG_FILE}: {exc})"


CHILD = Child()
