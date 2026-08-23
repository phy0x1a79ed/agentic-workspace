"""Supervision of the DeepSeek Harness (``dsh``) web server.

The harness is a node application that binds plain HTTP on loopback with no
auth of its own. That is the right default and we keep it: the mesh-facing half
is a separate front (see ``front``), and nothing here widens the bind.

Unlike ``claude-science``, this child is **parented**. The harness persists its
sessions to ``$DSH_HOME``, so an awm restart costs a browser reconnect rather
than a conversation, and parenthood is what makes one supervised lifetime cover
the whole stack — no adoption protocol, no orphan to find later. The child is
put in its own session so the whole node process tree can be signalled as a
group; ``PR_SET_PDEATHSIG`` is what closes the window where this process dies
between the fork and the child's first instruction.

**Where things live.** The harness is not installed from a registry: it is a
fork held as its own awm project, and the ``release`` worktree of
``projects/deepseek-harness`` *is* the runnable harness. This module launches
the CLI that worktree's own build emits, so the code serving the browser is the
code in that git history — which is what makes a change to the harness
reviewable and promotable instead of an edit to a build artifact nobody tracks.
``DSH_FORK_DIR`` points a dev sandbox at the ``dev`` worktree instead.

The harness's own data directory stays *state*, under ``.awm/services/dsh/``
beside every other service's, because sessions and ``settings.yaml`` are
per-node runtime data. ``install.sh`` records the absolute node bin directory
in ``node-bin`` there too, because the supervisor respawns under systemd's
minimal PATH where neither ``node`` nor ``mamba`` exists.

**The model route.** The OpenRouter key is read out of opencode's ``auth.json``
at spawn and passed as ``OPENROUTER_API_KEY``, which is the name
``settings.yaml`` declares via ``apiKeyEnv``. The key therefore stays in the one
file that already owns it: it is never copied into a settings file, a git
object, or this service's own state.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import yaml

from awm.config import AWM_DIR, WORKSPACE_ROOT

log = logging.getLogger("awm.dsh.harness")

HERE = Path(__file__).resolve().parent          # …/dsh/awm/dsh
SERVICE_DIR = HERE.parents[1]                   # …/awm/services/dsh

#: Per-service state: the harness's data dir and its log.
STATE_DIR = Path(os.environ.get("DSH_STATE_DIR") or (AWM_DIR / "services" / "dsh"))
DSH_HOME = Path(os.environ.get("DSH_HOME") or (STATE_DIR / "home"))
NODE_BIN_FILE = STATE_DIR / "node-bin"
LOG_FILE = STATE_DIR / "dsh.log"
SETTINGS_FILE = DSH_HOME / "settings.yaml"

#: The harness fork worktree. The deployed service serves ``release``; a dev
#: sandbox overrides this to ``dev`` in its gitignored ``gateway/dev/.env``.
FORK_DIR = Path(os.environ.get("DSH_FORK_DIR")
                or (WORKSPACE_ROOT / "projects" / "deepseek-harness" / "release"))

#: The CLI ``pnpm run build`` emits — ``apps/cli`` declares ``bin: lib/bin.js``.
#: Launched as an argument to an absolute node rather than through its shebang,
#: because the supervisor respawns under systemd's minimal PATH.
CLI = FORK_DIR / "apps" / "cli" / "lib" / "bin.js"
PKG_JSON = FORK_DIR / "package.json"

#: What ``install.sh`` last built, written where the worktree already ignores
#: it. Reported beside the live revision so a stale build is visible rather
#: than something you infer from a symptom.
BUILD_STAMP = FORK_DIR / ".awm" / "dsh-build-stamp"

#: Loopback port for the harness. Not the mesh port — the front owns that.
PORT = int(os.environ.get("DSH_UPSTREAM_PORT", "12311"))

#: The directory the harness is launched from, and so the root of the workspace
#: picker it offers. The whole awm workspace, so any project is selectable.
WORKDIR = Path(os.environ.get("DSH_WORKDIR") or WORKSPACE_ROOT)

HEALTH_INTERVAL_S = float(os.environ.get("DSH_HEALTH_INTERVAL_S", "20"))
START_TIMEOUT_S = float(os.environ.get("DSH_START_TIMEOUT_S", "60"))

#: opencode's credential store — the single place the OpenRouter key lives.
AUTH_JSON = Path(os.environ.get("DSH_AUTH_JSON")
                 or (Path.home() / ".local/share/opencode/auth.json"))

#: The provider id the settings file declares, and the env var it points at.
PROVIDER_ID = "openrouter"
API_KEY_ENV = "OPENROUTER_API_KEY"

#: Settings sections are keyed by the *plugin id* in the composed profile, which
#: is where these two names come from — ``dsh --profile web --dump-config``
#: lists them. Guessing either produces a file the harness reads and ignores.
#: They live here rather than in ``settings`` so this module can report on the
#: document without importing the module that writes it.
PLUGIN_KEY = "llm-pi-ai"
DEFAULT_MODEL_KEY = "agent-default-model"


def installed() -> bool:
    """Whether the fork worktree has been built. A checkout alone is not enough."""
    return CLI.is_file()


def version() -> str | None:
    """The harness version the fork declares, or None if it is absent."""
    try:
        return json.loads(PKG_JSON.read_text()).get("version")
    except (OSError, ValueError):
        return None


def _git(*args: str) -> str | None:
    """A git command in the fork worktree, or None if it cannot be answered."""
    try:
        r = subprocess.run(["git", "-C", str(FORK_DIR), *args],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def source_state() -> dict[str, Any]:
    """Which harness revision is on disk, and whether the build matches it.

    Without this a stale or hand-edited deployment is invisible: the harness
    serves whatever bundles happen to be in ``lib/``, and nothing relates them
    to a commit. ``dirty`` and ``built_current`` are the two ways that goes
    wrong — an uncommitted edit, and a commit that was never rebuilt.
    """
    status = _git("status", "--porcelain")
    stamp = None
    try:
        stamp = dict(
            line.split("=", 1) for line in
            BUILD_STAMP.read_text().split() if "=" in line
        )
    except (OSError, ValueError):
        pass
    head = _git("rev-parse", "HEAD")
    return {
        "dir": str(FORK_DIR),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "describe": _git("describe", "--tags", "--always", "--dirty"),
        "head": head,
        "dirty": None if status is None else bool(status),
        "built_head": (stamp or {}).get("head"),
        "built_current": (None if (stamp is None or head is None)
                          else stamp.get("head") == head
                          and stamp.get("dirty") == ("1" if status else "0")),
    }


def node_exe() -> str:
    """Absolute node, falling back to PATH. The CLI is a script we pass to it."""
    nb = node_bin_dir()
    return str(Path(nb) / "node") if nb else "node"


def node_bin_dir() -> str | None:
    """The absolute node bin directory ``install.sh`` recorded, if any.

    Read from a file rather than inherited from the environment because the
    gateway respawns this service under systemd's minimal PATH, where the
    interactive shell's node — and ``mamba``, which could find it — are absent.
    """
    try:
        value = NODE_BIN_FILE.read_text().strip()
    except OSError:
        return None
    return value or None


def openrouter_key() -> str | None:
    """The OpenRouter key from opencode's auth store, or None.

    Never logged, never written anywhere: it is read here and handed straight
    to the child's environment.
    """
    try:
        entry = json.loads(AUTH_JSON.read_text()).get(PROVIDER_ID) or {}
    except (OSError, ValueError):
        return None
    return (entry.get("key") or "").strip() or None


def listening(port: int = PORT, *, timeout: float = 0.5) -> bool:
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _preexec() -> None:  # pragma: no cover — runs in the forked child
    """Own session (so the tree is one signalable group) + die with the parent.

    Order matters: ``setsid`` first, then the death signal, then a re-check
    that the parent is still alive — a parent that exited between the fork and
    ``prctl`` would leave a child nothing will ever signal.
    """
    os.setsid()
    try:
        import ctypes

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:  # noqa: BLE001 — best effort; the group kill still works
        pass
    if os.getppid() == 1:
        os._exit(1)


class Supervisor:
    """Owns exactly one harness process. Every method is safe to call at any time."""

    def __init__(self) -> None:
        # Reentrant: the lifecycle methods hold it and then call snapshot(),
        # which takes it again to read a consistent view.
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
            return {
                "installed": installed(),
                "version": version(),
                "running": self._alive(),
                "pid": proc.pid if proc and proc.poll() is None else None,
                "exit_code": proc.poll() if proc else None,
                "port": PORT,
                "listening": listening(),
                "uptime_s": (round(time.time() - self._started_at, 1)
                             if self._started_at and self._alive() else None),
                "home": str(DSH_HOME),
                "runtime": str(FORK_DIR),
                "workdir": str(WORKDIR),
                "node_bin": node_bin_dir(),
                "source": source_state(),
                "model_route": self.route_state(),
                "error": self._last_error,
            }

    def route_state(self) -> dict[str, Any]:
        """Whether the model route is usable, in the three parts that can fail.

        A declared provider with no key behind it, a key with no provider
        declared, and a working provider the default selection does not point
        at all present in the GUI the same way — nothing answers — so they are
        reported apart. The last is the easiest to miss: the profile ships a
        DeepSeek-direct default, and leaving it selected fails every request
        against a credential this workspace does not hold.
        """
        try:
            doc = yaml.safe_load(SETTINGS_FILE.read_text()) or {}
        except (OSError, ValueError, yaml.YAMLError):
            doc = {}
        if not isinstance(doc, dict):
            doc = {}
        providers = ((doc.get(PLUGIN_KEY) or {}).get("providers") or {})
        selected = doc.get(DEFAULT_MODEL_KEY) or {}
        catalog = [m.get("id") for m in
                   ((providers.get(PROVIDER_ID) or {}).get("models") or [])
                   if m.get("id")]
        return {
            "settings": str(SETTINGS_FILE),
            "provider_declared": PROVIDER_ID in providers,
            "models": catalog,
            "default_model": (f"{selected.get('provider')}/{selected.get('model')}"
                              if selected else None),
            "default_model_ours": selected.get("provider") == PROVIDER_ID,
            "key_env": API_KEY_ENV,
            "key_available": openrouter_key() is not None,
            "key_source": str(AUTH_JSON),
        }

    # -- lifecycle ----------------------------------------------------------

    def _spawn(self) -> None:
        """Launch the harness. Caller holds the lock."""
        if not installed():
            raise FileNotFoundError(
                f"the harness fork at {FORK_DIR} has no built CLI at {CLI} — "
                f"run awm/services/dsh/install.sh")

        env = dict(os.environ)
        env["DSH_HOME"] = str(DSH_HOME)
        env["HOME"] = str(Path.home())
        nb = node_bin_dir()
        if nb:
            env["PATH"] = nb + os.pathsep + env.get("PATH", "")
        key = openrouter_key()
        if key:
            env[API_KEY_ENV] = key
        else:
            log.warning("dsh: no OpenRouter key in %s — the harness will start "
                        "but its model picker will be empty", AUTH_JSON)

        DSH_HOME.mkdir(parents=True, exist_ok=True)
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        cmd = [node_exe(), str(CLI), "--profile", "web", "--host", "127.0.0.1",
               "--port", str(PORT), "--no-open"]
        log.info("dsh: launching %s (cwd=%s)", " ".join(cmd), WORKDIR)
        # Append rather than truncate: the log is the only account of a crash
        # that happened between two `status` calls.
        out = open(LOG_FILE, "ab", buffering=0)  # noqa: SIM115 — owned by the child
        try:
            self._proc = subprocess.Popen(
                cmd, cwd=str(WORKDIR), env=env,
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
        deadline = time.time() + START_TIMEOUT_S
        while time.time() < deadline:
            if listening():
                return
            if not self._alive():
                self._last_error = (
                    f"the harness exited with {self._proc.poll() if self._proc else '?'} "
                    f"before binding {PORT}; see {LOG_FILE}")
                return
            time.sleep(0.4)
        self._last_error = f"the harness did not bind {PORT} within {START_TIMEOUT_S}s"

    def stop(self, *, timeout: float = 15.0) -> dict[str, Any]:
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                self._proc = None
                return {"action": "not-running", "running": False}
            # The whole group: the CLI shim, node, and anything node spawned.
            # Signalling the pid alone leaves the tree holding the port.
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
        """Respawn the harness if it has actually died. Cheap; called on a loop."""
        with self._lock:
            if self._alive() or not installed():
                return {"action": "none"}
            code = self._proc.poll() if self._proc else None
            try:
                self._spawn()
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"{type(exc).__name__}: {exc}"
                return {"action": "respawn-failed", "error": self._last_error}
        return {"action": "respawned", "previous_exit": code}

    # -- diagnostics --------------------------------------------------------

    def logs(self, tail: int = 200) -> str:
        try:
            with open(LOG_FILE, "rb") as fh:
                return b"".join(fh.readlines()[-tail:]).decode("utf-8", "replace")
        except OSError as exc:
            return f"(no log at {LOG_FILE}: {exc})"
