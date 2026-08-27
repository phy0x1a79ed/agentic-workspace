"""Supervision of the one Trilium node process that serves the shared vault.

The child binds plain HTTP on loopback with no transport security and, on this
deployment, **no authentication of its own**. Both are deliberate and they are
the same decision: an awm edge listener authenticates the session and proxies
the survivors, so the child is never reachable by anything that has not already
signed in. See `child_env` for the invariant that makes it safe, and
`instances.EDGE_ONLY` for the one knob that turns it off.

**The child is parented.** Trilium persists everything to its data directory,
so an awm restart costs a browser reload rather than any content. Parenthood is
what makes one supervised lifetime cover the whole stack — no adoption
protocol, and no orphan to find later. The child is put in its own session so
its whole process tree can be signalled as a group, and `PR_SET_PDEATHSIG`
closes the window where this process dies between the fork and the child's
first instruction.

**Where things live.** The vault's content is in the `vault` project's worktree,
never in service state — see `instances`. Service state holds only the recorded
node bin, the tarball fallback, and these logs.
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
from awm.trilium.instances import Vault

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


def child_env(vault: Vault) -> dict[str, str]:
    """The environment the child runs under.

    Every value here is a decision, so they are named rather than templated:

    - The data directory is the vault project's worktree, which is what puts
      the shared knowledge base on one branch with its own history.
    - The backup directory is set explicitly so the rolling copies land beside
      the live database rather than inside the DVC-pinned chunk. Trilium
      rewrites `backup-daily.db` in place, and a pinned file is a read-only
      hardlink into the shared cache — see `Vault.rolling_dir`.
    - `trustedReverseProxy` makes express read `X-Forwarded-For`, so Trilium's
      per-IP rate limiter sees the real client rather than every visitor
      collapsed onto 127.0.0.1. `loopback` rather than `true` because the edge
      always connects from there, and a blanket trust would let a forged header
      past anything that reads a client address. (It does *not* control the
      `Secure` flag on Trilium's session cookie, whatever an older comment here
      claimed: `session_parser.ts` uses a literal `config.Network.https`.)
    - Backend scripting and the SQL console are both off by default on a server
      build. They are set anyway: a `config.ini` in the data directory can turn
      either on, and on a public host both are arbitrary code execution.

    **The authentication setting, and the invariant it rests on.**
    `noAuthentication` stands down every guard Trilium has — the app shell, the
    internal API, the whole of ETAPI (`etapi_utils.ts` checks
    `noAuthentication || isValidAuthHeader`), the setup wizard's password gate,
    and the WebSocket's own `verifyClient`. What replaces them is not weaker but
    earlier: the awm edge authenticates the session before forwarding a byte, so
    the only way to reach this process is to have signed in already.

    That holds only while the edge is the *only* route in, which is why the bind
    below is loopback and asserted rather than merely set. `TRILIUM_HOST` is read
    by the fork ahead of every config path, so an inherited
    `TRILIUM_NETWORK_HOST` cannot move it. If you are adding a second route — an
    nginx location, another listener, a port forward — this variable has to go
    first.
    """
    env = dict(os.environ)
    env.update({
        "TRILIUM_ENV": "production",
        "TRILIUM_DATA_DIR": str(vault.data_dir),
        "TRILIUM_BACKUP_DIR": str(vault.rolling_dir),
        "TRILIUM_HOST": "127.0.0.1",
        "TRILIUM_PORT": str(instances.UPSTREAM_PORT),
        "TRILIUM_NETWORK_TRUSTEDREVERSEPROXY": "loopback",
        "TRILIUM_SECURITY_BACKEND_SCRIPTING_ENABLED": "false",
        "TRILIUM_SECURITY_SQL_CONSOLE_ENABLED": "false",
        "HOME": str(Path.home()),
    })
    if instances.EDGE_ONLY:
        env["TRILIUM_GENERAL_NOAUTHENTICATION"] = "true"
    else:
        env.pop("TRILIUM_GENERAL_NOAUTHENTICATION", None)
        log.warning("trilium: TRILIUM_EDGE_ONLY=0 — Trilium's own login is left "
                    "on; the edge is no longer assumed to be the only way in")
    # Upstream's `Network.host` defaults to 0.0.0.0 and is fed by
    # TRILIUM_NETWORK_HOST. `TRILIUM_HOST` beats it today (host.ts consults the
    # env var before the config), but that ordering is upstream's to change and
    # the cost of it changing is a public, unauthenticated vault. So the losing
    # lever is removed rather than merely out-ranked: there is one way to set
    # the bind, and it is set to loopback two lines up.
    env.pop("TRILIUM_NETWORK_HOST", None)
    # The invariant, asserted where it is set rather than documented and hoped
    # for. A child reachable off-loopback with authentication off is the one
    # failure this whole design must not have.
    assert env["TRILIUM_HOST"] == "127.0.0.1", \
        "the vault child must bind loopback: the edge is the only way in"
    nb = instances.NODE_BIN_FILE
    try:
        recorded = nb.read_text().strip()
    except OSError:
        recorded = ""
    if recorded:
        env["PATH"] = recorded + os.pathsep + env.get("PATH", "")
    return env


class Child:
    """The vault's node process. Every method is safe to call at any time."""

    def __init__(self, vault: Vault | None = None) -> None:
        self.vault = vault or instances.VAULT
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._started_at: float | None = None
        self._last_error: str | None = None
        self._provisioned: bool = False
        # Set while something needs this child to stay down. The supervision
        # loop respawns anything that is not alive, so without it a restore
        # races the loop: the child comes back between the stop and the file
        # swap, and the swap then refuses because the port is bound again.
        self._held = False

    # -- state --------------------------------------------------------------

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def snapshot(self, *, verbose: bool = True) -> dict[str, Any]:
        """What the vault is doing.

        `verbose=False` drops the pid, the port and the absolute paths. A
        collaborator reading the page needs "up, initialised, N copies"; the
        rest is an operator's business and gratuitous on a public host.
        """
        with self._lock:
            proc = self._proc
            vault = self.vault
            state: dict[str, Any] = {
                "running": self._alive(),
                "listening": instances.listening(instances.UPSTREAM_PORT),
                "initialized": self._provisioned or None,
                "uptime_s": (round(time.time() - self._started_at, 1)
                             if self._started_at and self._alive() else None),
                "backups": self.backups(),
                "error": self._last_error,
            }
            if verbose:
                state.update({
                    "pid": proc.pid if proc and proc.poll() is None else None,
                    "exit_code": proc.poll() if proc else None,
                    "port": instances.UPSTREAM_PORT,
                    "scope": str(vault.scope),
                    "data_dir": str(vault.data_dir),
                    "log": str(vault.log_file),
                })
            return state

    def backups(self) -> list[str]:
        """The rolling database copies Trilium keeps, in name order.

        Reported because "the service is up" and "there is a recoverable copy"
        are different facts, and only the second one matters the day someone
        needs it. These are Trilium's own rotation — the named snapshots awm
        pins are listed by `trilium snapshots`.
        """
        try:
            return sorted(p.name for p in self.vault.rolling_dir.glob("backup-*.*"))
        except OSError:
            return []

    # -- lifecycle ----------------------------------------------------------

    def _spawn(self) -> None:
        """Launch the vault's server. Caller holds the lock."""
        chosen = instances.entry_point()
        if chosen is None:
            raise FileNotFoundError(
                f"no Trilium server bundle: neither {instances.FORK_ENTRY} nor "
                f"{instances.TARBALL_ENTRY} exists — run "
                f"awm/services/trilium/install.sh")
        entry, _source = chosen

        vault = self.vault
        if not vault.exists:
            raise FileNotFoundError(
                f"no vault worktree at {vault.scope} — create it with "
                f"`awm scope create --project vault --scope main`")
        vault.data_dir.mkdir(parents=True, exist_ok=True)
        vault.rolling_dir.mkdir(parents=True, exist_ok=True)
        vault.log_file.parent.mkdir(parents=True, exist_ok=True)

        # An absolute path to the bundle, and the cwd is irrelevant: in
        # production Trilium derives its resource directory from
        # `path.dirname(process.argv[1])`, so the assets are found relative to
        # the bundle rather than to wherever the supervisor happened to be.
        cmd = [instances.node_exe(), str(entry)]
        log.info("trilium: launching %s (port %d, data %s)",
                 " ".join(cmd), instances.UPSTREAM_PORT, vault.data_dir)
        # Append rather than truncate: the log is the only account of a crash
        # that happened between two `status` calls.
        out = open(vault.log_file, "ab", buffering=0)  # noqa: SIM115 — owned by the child
        try:
            self._proc = subprocess.Popen(
                cmd, cwd=str(entry.parent), env=child_env(vault),
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
            self.provision()
        return self.snapshot() | {"action": "started"}

    def provision(self) -> dict[str, Any]:
        """Give the vault a database if it has none, and settle its launchbar.

        Never fatal. Deferred to here rather than to `add-user.sh` because a
        scope can exist long before a gateway does, and a script would have to
        know a port. The probe is what makes it idempotent, and Trilium refuses
        a second attempt on its own, so a racing supervision tick costs a
        wasted request.

        The launchbar step runs on every start rather than once, because it is
        a *setting* Trilium keeps in the database where anyone can undo it —
        see `provision.hide_day_note_launchers`.
        """
        from awm.trilium import provision as _provision

        try:
            result = _provision.ensure_document()
        except Exception as exc:  # noqa: BLE001 — a broken vault must still report
            self._last_error = f"provision: {type(exc).__name__}: {exc}"
            log.warning("trilium: provisioning failed: %s", self._last_error)
            return {"action": "provision-failed", "error": self._last_error}
        self._provisioned = bool(result.get("initialized"))
        if self._provisioned:
            result = result | {"launchbar": _provision.hide_day_note_launchers()}
        return result

    def _await_listening(self) -> None:
        deadline = time.time() + instances.START_TIMEOUT_S
        port = instances.UPSTREAM_PORT
        while time.time() < deadline:
            if instances.listening(port):
                return
            if not self._alive():
                self._last_error = (
                    f"exited with {self._proc.poll() if self._proc else '?'} "
                    f"before binding {port}; see {self.vault.log_file}")
                return
            time.sleep(0.4)
        self._last_error = (f"did not bind {port} within "
                            f"{instances.START_TIMEOUT_S}s")

    def stop(self, *, timeout: float = 20.0, hold: bool = False) -> dict[str, Any]:
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
        """Respawn if the child has actually died, and provision if it has not
        yet been. Cheap; called on a loop."""
        with self._lock:
            if self._alive():
                if not self._provisioned and instances.listening(instances.UPSTREAM_PORT):
                    self.provision()
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
            with open(self.vault.log_file, "rb") as fh:
                return b"".join(fh.readlines()[-tail:]).decode("utf-8", "replace")
        except OSError as exc:
            return f"(no log at {self.vault.log_file}: {exc})"


#: The one child this service supervises. There is no fleet: one vault, one
#: process, and nothing to keep in step with a directory of scopes.
CHILD = Child()
