"""Keep this host's tether binary running.

One service folder, two kinds of host: the operator's node runs
``tether-operator``, the public host runs ``tether-relay``. Which one is
``paths.ROLE``, and the two are kept differently.

**The relay is parented.** It is put in its own session so the whole tree can
be signalled as a group, and ``PR_SET_PDEATHSIG`` closes the window where this
process dies between the fork and the child's first instruction.

**The operator daemon is adopted, not owned.** Sessions are sockets held by
tasks inside it, and the gateway restarts this service on every deploy, so a
daemon parented here ended every live session several times an hour. It runs
instead in a transient unit of the user's systemd manager. Detaching alone
would not do: ``systemctl restart awm`` kills by control group, and every
descendant of the gateway inherits that group however it forks. On start the
adapter attaches to a daemon that answers on the control socket and launches
one only when none does. It replaces a running daemon in two cases only: the
binary under it was replaced by a new release and no session is live, or the
``restart`` verb asks. Stopping the service leaves the daemon running. A
reboot ends it, because the unit is transient. Nothing about the owner's side
changes: either person can still cut a session, and the owner's client keeps
nothing.

Without a user manager the daemon starts in its own session instead. It then
outlives a restart of this service but not a cgroup kill of the gateway, and
``status`` says so with a null ``unit``.

The supervision loop calls ``reconcile`` from a thread, because a launch waits
for the socket to answer, and the gateway reaps a service whose event loop
stalls. See ARCHITECTURE.md § *The ready-ASAP contract*.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from awm.tether import paths

log = logging.getLogger("awm.tether.daemon")

#: How often the supervised loop asks whether the daemon is still there.
TICK_S = 5.0

#: How long a fresh daemon has to answer on its socket before the launch is
#: called a failure.
START_TIMEOUT_S = 15.0

#: How long one status probe may take. A daemon that accepts and does not
#: answer in time is busy, not dead.
PROBE_TIMEOUT_S = 5.0

#: The ceiling on the wait between failed launches, so a binary that exits on
#: start is retried every few minutes rather than every tick.
MAX_BACKOFF_S = 300.0

#: A daemon that dies younger than this counts as a failed launch, so one that
#: binds its socket and then exits backs off instead of cycling every tick.
SETTLE_S = 60.0

#: Variables systemd sets for the unit itself. Copying the gateway's values
#: would point the daemon at the gateway's journal stream and invocation.
_SYSTEMD_OWN = frozenset({
    "INVOCATION_ID", "JOURNAL_STREAM", "NOTIFY_SOCKET", "LISTEN_PID",
    "LISTEN_FDS", "LISTEN_FDNAMES", "MANAGERPID", "SYSTEMD_EXEC_PID",
    "WATCHDOG_PID", "WATCHDOG_USEC", "MAINPID",
})
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _preexec() -> None:  # pragma: no cover — runs in the forked child
    """Own session, die with the parent, and refuse to be orphaned.

    Order matters: ``setsid`` first so the tree is one signalable group, then
    the death signal, then a re-check — a parent that exited between the fork
    and ``prctl`` would leave a child nothing is ever going to signal.
    """
    os.setsid()
    try:
        import ctypes

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:  # noqa: BLE001 — best effort; the group kill still works
        pass
    if os.getppid() == 1:
        os._exit(1)


def _env() -> dict[str, str]:
    """The daemon's environment.

    Inherited wholesale, because what the daemon needs beyond the socket path
    — which relay this fleet uses, the bearer that reaches its authenticated
    half, a proxy — lives in the workspace env file and is already in this
    process. Naming each variable here would mean editing this file every time
    one is added.
    """
    env = dict(os.environ)
    env["TETHER_CONTROL_SOCKET"] = str(paths.CONTROL_SOCKET)
    return env


def _private_dir() -> None:
    paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
    paths.STATE_DIR.chmod(0o700)


def probe(timeout: float = PROBE_TIMEOUT_S) -> tuple[str, dict[str, Any]]:
    """Ask the daemon for its status: ``absent``, ``busy`` or ``answering``.

    Synchronous on purpose: it runs in the supervision thread and in places
    with no event loop. Busy counts as alive, because replacing a daemon that
    is slow to answer would end the sessions this arrangement keeps.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(paths.CONTROL_SOCKET))
    except OSError:
        sock.close()
        return "absent", {}
    try:
        sock.sendall(json.dumps({"verb": "status", "args": {}}).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    except OSError:
        return "busy", {}
    finally:
        sock.close()
    try:
        return "answering", json.loads(buf)
    except ValueError:
        return "busy", {}


def live_sessions(report: dict[str, Any]) -> list[dict]:
    return [s for s in report.get("sessions") or [] if s.get("phase") != "ended"]


def _user_manager_env() -> dict[str, str] | None:
    """Env for reaching this user's systemd manager, or ``None`` if there is none."""
    if not shutil.which("systemd-run"):
        return None
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    if not Path(runtime, "bus").is_socket():
        return None
    return {
        "XDG_RUNTIME_DIR": runtime,
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus",
    }


def _systemctl(bus: dict[str, str], *args: str,
               timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True,
                          text=True, timeout=timeout, env={**os.environ, **bus})


def _exe(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return None


def _runs(pid: int, binary: str | None) -> bool:
    """Whether ``pid`` is still the daemon, not a process that reused its number."""
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return True
    return bool(binary) and binary.encode() in argv


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _environment_file(env: dict[str, str]) -> str:
    """``env`` in systemd's EnvironmentFile syntax.

    Every value is double-quoted with ``\\`` and ``"`` escaped, which systemd
    249 and 255 both read back byte for byte, newlines and ``$`` included.
    """
    lines = []
    for key, value in sorted(env.items()):
        if key in _SYSTEMD_OWN or not _NAME.fullmatch(key):
            continue
        quoted = value.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{key}="{quoted}"')
    return "\n".join(lines) + "\n"


class Child:
    """The relay binary, parented to this process. Every method is safe to call."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._started_at: float | None = None
        self._error: str | None = None

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def snapshot(self) -> dict[str, Any]:
        """What this host can say about its child without asking the child."""
        binary = paths.role_binary()
        proc = self._proc
        return {
            "role": paths.ROLE,
            "binary": str(binary),
            "built": binary.is_file(),
            "built_at": (round(binary.stat().st_mtime) if binary.is_file() else None),
            "running": self.alive(),
            "pid": proc.pid if self.alive() else None,
            "exit_code": proc.poll() if proc else None,
            "uptime_s": (round(time.time() - self._started_at)
                         if self._started_at and self.alive() else None),
            "log": str(paths.DAEMON_LOG),
            "error": self._error,
        }

    def start(self) -> dict[str, Any]:
        if self.alive():
            return self.snapshot() | {"action": "already-running"}

        binary = paths.role_binary()
        if not binary.is_file():
            self._error = (
                f"{binary} is not present on this host; fetch-release.sh pulls "
                f"the binary pinned in RELEASE into {paths.BIN_DIR} on start, so "
                f"this usually means that fetch failed — see the service log"
            )
            return self.snapshot() | {"action": "not-built"}

        _private_dir()
        # Appended, never truncated: this log is the only account of a crash
        # that happened between two `status` calls.
        out = open(paths.DAEMON_LOG, "ab", buffering=0)  # noqa: SIM115 — the child owns it
        try:
            self._proc = subprocess.Popen(
                [str(binary)],
                cwd=str(paths.SERVICE_DIR),
                env=_env(),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                preexec_fn=_preexec,
            )
        except OSError as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            return self.snapshot() | {"action": "failed"}
        finally:
            out.close()

        self._started_at = time.time()
        self._error = None
        log.info("tether: started %s (pid %s)", binary.name, self._proc.pid)
        return self.snapshot() | {"action": "started"}

    def stop(self, *, timeout: float = 10.0) -> dict[str, Any]:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            self._proc = None
            return {"action": "not-running", "running": False}
        # The group, not the pid: the child may have spawned its own children
        # and signalling one of them leaves the rest holding the socket.
        self._signal_group(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._signal_group(proc, signal.SIGKILL)
            proc.wait(timeout=5)
        self._proc = None
        self._started_at = None
        return {"action": "stopped", "running": False}

    def reconcile(self) -> dict[str, Any]:
        """Start the child if it is not there. Cheap; called on a loop."""
        if self.alive():
            return {"action": "none"}
        previous = self._proc.poll() if self._proc else None
        if previous not in (None, 0):
            log.warning("tether: the %s child exited with %s; restarting — see %s",
                        paths.ROLE, previous, paths.DAEMON_LOG)
        return self.start() | {"previous_exit": previous}

    def restart(self, *, force: bool = False) -> dict[str, Any]:
        self.stop()
        return self.start()

    def shutdown(self) -> None:
        """The child dies with this process either way. Stopping it here makes a
        clean shutdown look clean in the log rather than like a kill."""
        self.stop()

    @staticmethod
    def _signal_group(proc: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                pass


class Adopted:
    """The operator daemon, attached to if it is running and launched if not.

    Holds almost nothing in memory. Whether a daemon is there is asked of its
    socket, and what was launched is read back from LAUNCH_FILE, so an adapter
    that has just restarted reaches the same answers as the one before it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._error: str | None = None
        self._adopted: bool | None = None
        self._pending: str | None = None
        self._failures = 0
        self._next_try = 0.0
        self._started: float | None = None

    # -- what is there -------------------------------------------------------

    def _launched(self) -> dict[str, Any]:
        try:
            return json.loads(paths.LAUNCH_FILE.read_text())
        except (OSError, ValueError):
            return {}

    def _unit(self, bus: dict[str, str] | None) -> dict[str, Any] | None:
        if bus is None:
            return None
        try:
            proc = _systemctl(bus, "show", f"{paths.OPERATOR_UNIT}.service",
                              "-p", "MainPID", "-p", "ActiveState", timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        fields = dict(line.partition("=")[::2] for line in proc.stdout.splitlines())
        return {
            "name": f"{paths.OPERATOR_UNIT}.service",
            "active_state": fields.get("ActiveState"),
            "main_pid": int(fields.get("MainPID") or 0) or None,
        }

    def pid(self, unit: dict[str, Any] | None = None) -> int | None:
        """The daemon's pid: the unit's main process, else what was launched."""
        if self._proc is not None:
            self._proc.poll()
        if unit and unit.get("main_pid"):
            return unit["main_pid"]
        launched = self._launched()
        pid = launched.get("pid")
        if isinstance(pid, int) and _alive(pid) and _runs(pid, launched.get("binary")):
            return pid
        return None

    def stale(self, pid: int | None) -> str | None:
        """Why the running daemon is not the release this host is pinned to.

        fetch-release.sh moves a new binary into place, so a daemon started
        from the old one reads its executable as deleted. The release stamped
        at launch catches the same change on a host whose /proc says less.
        """
        if not pid:
            return None
        exe = _exe(pid)
        if exe is not None and exe.endswith(" (deleted)"):
            return "its binary was replaced by a newer fetch"
        launched = self._launched()
        installed = paths.installed_release()
        if (launched.get("pid") == pid and launched.get("release") and installed
                and launched["release"] != installed):
            return f"it runs {launched['release']} and {installed} is installed"
        return None

    def alive(self) -> bool:
        return probe()[0] != "absent"

    def snapshot(self) -> dict[str, Any]:
        binary = paths.role_binary()
        bus = _user_manager_env()
        unit = self._unit(bus)
        pid = self.pid(unit)
        launched = self._launched()
        started = launched.get("started_at") if launched.get("pid") == pid else None
        return {
            "role": paths.ROLE,
            "binary": str(binary),
            "built": binary.is_file(),
            "built_at": (round(binary.stat().st_mtime) if binary.is_file() else None),
            "running": self.alive(),
            "pid": pid,
            "exit_code": self._proc.poll() if self._proc else None,
            "uptime_s": round(time.time() - started) if started else None,
            "log": str(paths.DAEMON_LOG),
            "error": self._error,
            # Null on a host with no user manager: there the daemon will not
            # outlive a restart of the gateway's systemd unit.
            "unit": unit,
            "adopted": self._adopted,
            "release": launched.get("release") if launched.get("pid") == pid else None,
            "replace_pending": self._pending,
        }

    # -- lifecycle -----------------------------------------------------------

    def reconcile(self) -> dict[str, Any]:
        """Attach to the daemon, launch one, or replace a stale one. Called on a loop."""
        if not self._lock.acquire(blocking=False):
            return {"action": "busy"}
        try:
            return self._reconcile()
        finally:
            self._lock.release()

    def _reconcile(self) -> dict[str, Any]:
        state, report = probe()
        now = time.monotonic()
        young = self._started is not None and now - self._started < SETTLE_S
        if state != "absent":
            if not young:
                self._failures = 0
            pid = self.pid(self._unit(_user_manager_env()))
            if self._adopted is None:
                self._adopted = True
                log.info("tether: attached to the running operator daemon (pid %s)", pid)
            reason = self.stale(pid)
            if reason is None:
                self._pending = None
                return {"action": "none"}
            if state != "answering" or live_sessions(report):
                if self._pending != reason:
                    log.info("tether: the daemon is due for replacement (%s); "
                             "waiting for its sessions to end", reason)
                self._pending = reason
                return {"action": "deferred", "reason": reason}
            log.info("tether: replacing the daemon, idle, because %s", reason)
            self._stop()
            out = self._start()
            if out["action"] == "started":
                self._started = time.monotonic()
                out["action"] = "replaced"
            return out | {"reason": reason}

        if young:
            self._started = None
            self._error = f"the daemon exited within {SETTLE_S:.0f}s of starting; see {paths.DAEMON_LOG}"
            self._back_off()
        if now < self._next_try:
            return {"action": "backoff", "error": self._error}
        out = self._start()
        if out["action"] == "started":
            self._started = time.monotonic()
        else:
            self._back_off()
        return out

    def _back_off(self) -> None:
        self._failures += 1
        wait = min(TICK_S * 2 ** self._failures, MAX_BACKOFF_S)
        self._next_try = time.monotonic() + wait
        log.warning("tether: the operator daemon is not running (%s); next try in %.0fs",
                    self._error, wait)

    def restart(self, *, force: bool = False) -> dict[str, Any]:
        """Replace the daemon, which ends every session it holds."""
        with self._lock:
            state, report = probe()
            live = live_sessions(report)
            if live and not force:
                return {"ok": False, "action": "refused",
                        "error": f"{len(live)} session(s) live; restarting ends them. "
                                 f"Pass force to do it anyway.",
                        "sessions": [s.get("slot") for s in live]}
            self._stop()
            self._failures = 0
            self._next_try = 0.0
            out = self._start()
            if out["action"] == "started":
                self._started = time.monotonic()
            return out

    def shutdown(self) -> None:
        """Leave the daemon running. That is the point of adopting it."""
        log.info("tether: leaving the operator daemon running for the next adapter")

    def _start(self) -> dict[str, Any]:
        binary = paths.role_binary()
        if not binary.is_file():
            self._error = (
                f"{binary} is not present on this host; fetch-release.sh pulls "
                f"the binary pinned in RELEASE into {paths.BIN_DIR} on start, so "
                f"this usually means that fetch failed — see the service log"
            )
            return {"action": "not-built", "error": self._error}

        _private_dir()
        bus = _user_manager_env()
        try:
            if bus is not None:
                self._launch_unit(binary, bus)
            else:
                self._launch_detached(binary)
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            return {"action": "failed", "error": self._error}

        deadline = time.monotonic() + START_TIMEOUT_S
        while probe(timeout=1.0)[0] == "absent":
            if time.monotonic() > deadline or (self._proc and self._proc.poll() is not None):
                self._error = (f"the daemon did not answer on {paths.CONTROL_SOCKET} "
                               f"within {START_TIMEOUT_S:.0f}s; see {paths.DAEMON_LOG}")
                return {"action": "failed", "error": self._error}
            time.sleep(0.2)

        pid = self.pid(self._unit(bus)) or (self._proc.pid if self._proc else None)
        record = {"pid": pid, "release": paths.installed_release(),
                  "binary": str(binary), "started_at": time.time(),
                  "unit": f"{paths.OPERATOR_UNIT}.service" if bus else None}
        tmp = paths.LAUNCH_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(record))
        tmp.replace(paths.LAUNCH_FILE)
        self._error = None
        self._adopted = False
        self._pending = None
        log.info("tether: started %s (pid %s) %s", binary.name, pid,
                 f"in user unit {record['unit']}" if bus else "in its own session")
        return {"action": "started", "pid": pid}

    def _launch_unit(self, binary: Path, bus: dict[str, str]) -> None:
        unit = paths.OPERATOR_UNIT
        # Only reached when nothing answers on the socket, so a unit still
        # holding the name is a corpse; systemd-run refuses the name until it
        # is cleared.
        for verb in ("stop", "reset-failed"):
            _systemctl(bus, verb, unit)
        fd = os.open(paths.ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(_environment_file(_env()))
        os.chmod(paths.ENV_FILE, 0o600)
        proc = subprocess.run(
            ["systemd-run", "--user", "--quiet", "--collect", f"--unit={unit}",
             "--property=Restart=no", "--property=TimeoutStopSec=15",
             f"--property=EnvironmentFile={paths.ENV_FILE}",
             f"--property=WorkingDirectory={paths.STATE_DIR}",
             f"--property=StandardOutput=append:{paths.DAEMON_LOG}",
             f"--property=StandardError=append:{paths.DAEMON_LOG}",
             "--", str(binary)],
            capture_output=True, text=True, timeout=60, env={**os.environ, **bus},
        )
        if proc.returncode != 0:
            raise RuntimeError(f"systemd-run failed ({proc.returncode}): "
                               f"{(proc.stderr or proc.stdout).strip()[:400]}")

    def _launch_detached(self, binary: Path) -> None:
        out = open(paths.DAEMON_LOG, "ab", buffering=0)  # noqa: SIM115 — the child owns it
        try:
            self._proc = subprocess.Popen(
                [str(binary)], cwd=str(paths.STATE_DIR), env=_env(),
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            out.close()

    def _stop(self, timeout: float = 15.0) -> None:
        bus = _user_manager_env()
        pid = self.pid(self._unit(bus))
        if bus is not None:
            _systemctl(bus, "stop", paths.OPERATOR_UNIT, timeout=timeout + 15)
        # A daemon outside the unit — launched with no user manager, or by an
        # adapter from before units — is not stopped by stopping the unit.
        if _alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + timeout
        while _alive(pid) and time.monotonic() < deadline:
            if self._proc is not None:
                self._proc.poll()
            time.sleep(0.2)
        if _alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self._proc is not None:
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            self._proc = None


def for_role() -> Child | Adopted:
    return Adopted() if paths.ROLE == paths.OPERATOR else Child()


def tail(path: Path, lines: int) -> list[str]:
    """The last lines of a log file, or an empty list if there is no file yet."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    return text.splitlines()[-max(lines, 1):]
