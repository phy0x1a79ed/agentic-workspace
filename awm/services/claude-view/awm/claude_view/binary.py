"""Own the upstream claude-view server process.

claude-view is a foreign Rust binary that we neither wrote nor patch: it walks
``~/.claude/projects/**/*.jsonl``, keeps a SQLite + tantivy index beside itself,
and serves a React SPA plus a JSON API on loopback. Everything in this module is
about making that binary behave like an awm-supervised component — found by
absolute path, launched with a pinned environment, health-probed, respawned, and
above all *never* left orphaned holding its port.

Three things here are load-bearing and easy to get wrong:

**The binary is located, never assumed.** The gateway runs ``bash run.sh`` on
systemd's minimal PATH, so ``claude-view`` is not on it and never will be. The
path is resolved relative to this file (``<service>/vendor/<version>/``), which
is also where the upstream ``dist/`` frontend sits — the binary's own asset
lookup is "``./dist`` beside the executable", so keeping that layout intact is
what makes the SPA load without a ``STATIC_DIR`` override.

**The environment is a containment boundary, not a preference.** Left to itself
the binary writes to ``~/.claude-view/``, registers hooks and a statusline into
``~/.claude/settings.json``, walks its port up by ten on a bind collision, and
opens a browser. Every one of those is switched off below. ``CLAUDE_VIEW_DATA_DIR``
is the single source of truth for all its write paths, so pointing it at
``<service>/state/`` confines the index, the port file, and the telemetry config
to a directory we can delete without consequence.

**A child that outlives its parent is the failure mode to design against.** An
orphaned server keeps port 47892 bound, so the respawn that follows fails to
bind and the service flaps. ``atexit`` covers the graceful path, but not
``SIGKILL`` — so the child also gets ``PR_SET_PDEATHSIG``, which has the *kernel*
signal it the moment this process dies by any means. Belt and braces, because
the graceful path is exactly the one that does not run when it matters.
"""

from __future__ import annotations

import atexit
import ctypes
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

log = logging.getLogger("awm.claude_view.binary")

HERE = Path(__file__).resolve().parent           # awm/services/claude-view/awm/claude_view
SERVICE_DIR = HERE.parents[1]                    # awm/services/claude-view
VENDOR_DIR = SERVICE_DIR / "vendor"
STATE_DIR = Path(os.environ.get("CLAUDE_VIEW_STATE_DIR") or (SERVICE_DIR / "state"))

#: The upstream release this service is pinned to. Bumping it is a deliberate
#: step: re-run the corpus gate (INSTALL.md) before trusting a new version.
PINNED_VERSION = os.environ.get("CLAUDE_VIEW_VERSION", "0.45.0")

#: Loopback port for the upstream server. Not the mesh-facing port — the HTTPS
#: front owns that and proxies here.
PORT = int(os.environ.get("CLAUDE_VIEW_UPSTREAM_PORT", "47892"))

#: How often the supervision loop probes /api/health.
HEALTH_INTERVAL_S = float(os.environ.get("CLAUDE_VIEW_HEALTH_INTERVAL_S", "20"))

#: Grace period between SIGTERM and SIGKILL when stopping the child.
STOP_GRACE_S = 10.0

_PR_SET_PDEATHSIG = 1


def install_dir() -> Path:
    """Directory holding the pinned ``claude-view`` binary and its ``dist/``."""
    override = os.environ.get("CLAUDE_VIEW_INSTALL_DIR")
    if override:
        return Path(override).expanduser()
    return VENDOR_DIR / f"v{PINNED_VERSION}"


def binary_path() -> Path:
    return install_dir() / "claude-view"


def installed() -> bool:
    """True once install.sh has produced a runnable binary + frontend."""
    d = install_dir()
    return (d / "claude-view").is_file() and (d / "dist" / "index.html").is_file()


def node_path() -> Path | None:
    """Absolute path to the ``node`` the upstream server needs, or None.

    The Rust server is not self-contained. Its ``/api/sidecar/*`` routes and
    the ``/ws/chat/*`` relay are proxies to a **Node sidecar** that the server
    spawns itself, on demand, as literally ``Command::new("node")`` — so it is
    resolved from ``PATH``, and the gateway runs services on systemd's minimal
    PATH where ``node`` does not exist. The failure is quiet and misleading:
    the SPA loads, the terminal works (that relay is Rust-native), and only
    the chat surface breaks, as a 503 with the send button greyed out.

    Worse, upstream's circuit breaker counts a failed spawn *before* it checks
    whether ``node`` exists, so ten dead attempts in forty seconds latch it
    open and every later request fails with a stale "circuit open" message
    that says nothing about the real cause.

    So resolve it the same way we resolve the server binary: explicitly, and
    preferring our own interpreter's ``bin/`` — the awm conda env ships node
    beside python, which makes this a dependency the service install already
    guarantees rather than one the host has to happen to satisfy.
    """
    override = os.environ.get("CLAUDE_VIEW_NODE")
    if override:
        p = Path(override).expanduser()
        return p if p.is_file() else None
    beside = Path(sys.executable).resolve().parent / "node"
    if beside.is_file():
        return beside
    found = shutil.which("node")
    return Path(found) if found else None


def child_env() -> dict[str, str]:
    """The pinned environment the upstream server runs under.

    Every entry is a containment or determinism decision; see the module
    docstring. Inherits the ambient environment so ``HOME``, ``PATH`` and the
    tmux variables the terminal feature needs still reach the child.
    """
    env = dict(os.environ)
    env.update({
        # Single source of truth for ALL claude-view write paths. Without this
        # the index, port file and telemetry config land in ~/.claude-view/.
        "CLAUDE_VIEW_DATA_DIR": str(STATE_DIR),
        # Do not touch ~/.claude/settings.json. This also makes the bind fail
        # fast on a busy port instead of silently walking up to port+10 and
        # killing whatever it decides is a "stale claude-view".
        "CLAUDE_VIEW_SKIP_HOOKS": "1",
        # Kill switch. Redundant on our source build (telemetry needs a
        # compile-time POSTHOG_API_KEY we never supply, so it resolves to
        # Disabled regardless) but set explicitly so the intent survives a
        # future switch to an official binary.
        "CLAUDE_VIEW_TELEMETRY": "0",
        # Headless host: never try to open a browser.
        "CLAUDE_VIEW_NO_OPEN": "1",
        "CLAUDE_VIEW_PORT": str(PORT),
        "RUST_LOG": os.environ.get("CLAUDE_VIEW_RUST_LOG", "info"),
    })
    # Deliberately NOT set: CLAUDE_VIEW_BIND_ADDR. The default is
    # 127.0.0.1, and loopback-only is the whole security model — the mesh sees
    # this server only through the authenticated HTTPS front.
    env.pop("CLAUDE_VIEW_BIND_ADDR", None)
    # The server shells out to a bare `node` for its sidecar (see node_path).
    # Prepending rather than replacing: the child also runs tmux, git and lsof
    # off PATH, and those belong to the host.
    node = node_path()
    if node is not None:
        env["PATH"] = os.pathsep.join([str(node.parent), env.get("PATH", "")])
    return env


def _pdeathsig() -> None:
    """Ask the kernel to SIGTERM this child when its parent dies (Linux).

    Runs in the forked child between ``fork`` and ``exec``. This is the only
    orphan guard that survives the parent being SIGKILLed, which is precisely
    the case ``atexit`` cannot cover.
    """
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(
            _PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    except Exception:  # noqa: BLE001 — best-effort; atexit still covers the rest
        pass
    os.setpgrp()  # own process group, so we can signal the whole tree


class Supervisor:
    """Spawn, health-check, and respawn the upstream server.

    Thread-safe: the health loop and RPC handlers both touch it. All state
    lives behind one lock and every accessor is non-raising, because the
    ``status`` verb has to be able to report *why* the thing is broken.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._started_at: float | None = None
        self._restarts = 0
        self._last_error: str | None = None
        self._version: str | None = None
        self._stopping = False
        atexit.register(self.stop)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> dict:
        """Spawn the child if it is not already running. Idempotent."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return self._snapshot_locked()
            if not installed():
                self._last_error = (
                    f"claude-view not installed at {install_dir()}; run install.sh")
                log.warning("claude-view: %s", self._last_error)
                return self._snapshot_locked()

            STATE_DIR.mkdir(parents=True, exist_ok=True)
            logfile = STATE_DIR / "server.log"
            try:
                # Log to a file rather than a pipe: nothing drains a pipe here,
                # and a full pipe buffer would wedge the child mid-write.
                fh = open(logfile, "ab", buffering=0)
                self._proc = subprocess.Popen(
                    [str(binary_path())],
                    cwd=str(install_dir()),
                    env=child_env(),
                    stdin=subprocess.DEVNULL,
                    stdout=fh, stderr=fh,
                    preexec_fn=_pdeathsig,
                )
                self._started_at = time.time()
                self._last_error = None
                log.info("claude-view: spawned pid=%d port=%d data=%s",
                         self._proc.pid, PORT, STATE_DIR)
            except Exception as exc:  # noqa: BLE001
                self._proc = None
                self._last_error = f"{type(exc).__name__}: {exc}"
                log.exception("claude-view: spawn failed")
            return self._snapshot_locked()

    def stop(self) -> dict:
        """Stop the child's whole process group, escalating to SIGKILL.

        Signals the *group*, not the pid: the server spawns a node sidecar and
        tmux helpers, and killing only the leader leaves those holding
        resources. Registered with ``atexit``, so it runs on a clean exit; the
        ``PR_SET_PDEATHSIG`` set at spawn covers the unclean ones.
        """
        with self._lock:
            self._stopping = True
            proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return {"stopped": True}
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return {"stopped": True}
        except Exception:  # noqa: BLE001 — group may be gone; fall through
            pass
        try:
            proc.wait(timeout=STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            log.warning("claude-view: pid %d ignored SIGTERM; SIGKILL", proc.pid)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.error("claude-view: pid %d survived SIGKILL", proc.pid)
        return {"stopped": True}

    def alive(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def reconcile(self) -> dict:
        """One supervision pass: respawn a dead child, otherwise no-op."""
        with self._lock:
            if self._stopping:
                return {"action": "stopping"}
            dead = self._proc is None or self._proc.poll() is not None
            if not dead:
                return {"action": "none"}
            if self._proc is not None:
                code = self._proc.returncode
                self._restarts += 1
                log.warning("claude-view: child exited rc=%s — respawning "
                            "(restart #%d)", code, self._restarts)
                self._proc = None
        self.start()
        return {"action": "respawned"}

    # -- probes ------------------------------------------------------------

    def health(self, timeout: float = 5.0) -> dict:
        """Probe the upstream ``/api/health``. Never raises."""
        try:
            r = httpx.get(f"http://127.0.0.1:{PORT}/api/health", timeout=timeout)
            body: object = None
            try:
                body = r.json()
            except Exception:  # noqa: BLE001 — non-JSON body is still a signal
                body = (r.text or "")[:200]
            return {"ok": r.status_code == 200, "status": r.status_code, "body": body}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def version(self) -> str | None:
        """``claude-view --version``, cached. None if it cannot be determined."""
        with self._lock:
            if self._version is not None:
                return self._version
        if not installed():
            return None
        try:
            out = subprocess.run(
                [str(binary_path()), "--version"],
                capture_output=True, text=True, timeout=15, env=child_env(),
            )
            v = (out.stdout or out.stderr).strip() or None
        except Exception:  # noqa: BLE001
            v = None
        with self._lock:
            self._version = v
        return v

    # -- status ------------------------------------------------------------

    def _snapshot_locked(self) -> dict:
        proc = self._proc
        running = proc is not None and proc.poll() is None
        return {
            "running": running,
            "pid": proc.pid if running else None,
            "uptime_s": (round(time.time() - self._started_at, 1)
                         if running and self._started_at else None),
            "restarts": self._restarts,
            "last_error": self._last_error,
        }

    def snapshot(self) -> dict:
        with self._lock:
            snap = self._snapshot_locked()
        node = node_path()
        snap.update({
            "pinned_version": PINNED_VERSION,
            "binary": str(binary_path()),
            "installed": installed(),
            "upstream_port": PORT,
            "data_dir": str(STATE_DIR),
            # Reported because its absence degrades the service silently: chat
            # dies, everything else keeps working. See node_path().
            "node": str(node) if node else None,
            "sidecar_capable": node is not None,
        })
        return snap


def index_state() -> dict:
    """Size of the on-disk index — the cheapest honest proxy for "indexed yet".

    Reported rather than interpreted: the upstream server has no single
    "index complete" flag, so ``status`` shows the DB size and lets the caller
    judge. Never raises.
    """
    out: dict = {"data_dir": str(STATE_DIR), "exists": STATE_DIR.is_dir()}
    try:
        db = STATE_DIR / "claude-view.db"
        out["db_bytes"] = db.stat().st_size if db.is_file() else 0
        total = 0
        for p in STATE_DIR.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
        out["total_bytes"] = total
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out
