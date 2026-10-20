"""Keep this node's Claude Code daemon running, outside awm and every terminal.

Every background session on a node is a child of the daemon, and lives in the
daemon's control group. A daemon started on demand by `claude agents` lives in
that terminal's scope, so an OOM kill anywhere in the scope stops the daemon
and every session with it. A daemon started from inside the gateway would die
on the next deploy. So the daemon runs in a transient unit of the user's
systemd manager, `awm-claude-daemon.service`, launched from here:

- ``KillMode=process``: the sessions outlive the daemon's own stops and its
  self-upgrades, which exit cleanly and rely on ``Restart=always``.
- ``RestartSteps`` and ``RestartMaxDelaySec``: a daemon that keeps failing
  backs off from 10 s to 10 min, and ``StartLimitIntervalSec=0`` means it is
  never given up for good. systemd keeps counting restarts across healthy runs,
  so a daemon that has stayed up for ``SETTLE_S`` gets its count reset here.
  Without that, a few weeks of upgrades leave every restart waiting 10 minutes.
- ``OOMPolicy=continue``: one OOM-killed session does not stop the unit.
- ``OOMScoreAdjust=-200``: the kernel picks the real memory hog first.

A daemon started with ``--origin service`` asks a running transient daemon to
yield over its control socket, and the transient one exits leaving its
sessions running. When that socket is unreachable the handover fails, the new
daemon exits, and systemd retries until the transient daemon is gone. That
case is resolved here with a SIGTERM to the transient daemon alone, which also
leaves its sessions running.

A transient unit ends with a reboot. This service starts with the gateway at
boot and launches the unit again. Without a user manager nothing is launched,
because the only alternative is the gateway's own control group.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from awm.claudedaemon import launch, roster

log = logging.getLogger("awm.keep.daemon")

UNIT = os.environ.get("AWM_KEEP_UNIT") or "awm-claude-daemon"
TICK_S = 30.0
SETTLE_S = 600.0
DISPLACE_S = 90.0
ENSURE_TIMEOUT_S = 30.0
KICK_EVERY_S = 60.0
HISTORY = 10

UNIT_PROPERTIES = (
    "KillMode=process",
    "Restart=always",
    "RestartSec=10s",
    "RestartSteps=6",
    "RestartMaxDelaySec=10min",
    "StartLimitIntervalSec=0",
    "OOMPolicy=continue",
    "OOMScoreAdjust=-200",
    "TimeoutStopSec=30",
)

_START = re.compile(r"^\[(?P<at>[^\]]+)\] \[supervisor\] ─── daemon start ─── "
                    r"version=(?P<version>\S+) pid=(?P<pid>\d+) origin=(?P<origin>\S+)")
_STOP = re.compile(r"^\[(?P<at>[^\]]+)\] \[supervisor\] shutting down "
                   r"\(cause=(?P<cause>[^,)]+)(?:, uptime=(?P<uptime>\d+)s)?")
_SHOW = ("LoadState", "ActiveState", "SubState", "MainPID", "NRestarts", "Result",
         "ExecMainCode", "ExecMainStatus", "MemoryHigh", "MemoryMax")


def enabled() -> str | None:
    """Why this process must leave the daemon alone, or None if it may keep it."""
    if os.environ.get("AWM_KEEP", "1") == "0":
        return "switched off (AWM_KEEP=0)"
    if os.environ.get("AWM_SERVICE_OVERLAY"):
        return "running as an overlay; the base keeps the daemon"
    if not Path(claude_bin()).is_file():
        return f"no Claude Code binary at {claude_bin()}"
    return None


def claude_bin() -> str:
    return os.environ.get("AWM_KEEP_CLAUDE") or launch.default_claude_bin()


def state_dir() -> Path:
    from awm.config import SERVICES_DIR
    return SERVICES_DIR / "keep"


def daemon_log() -> Path:
    return roster.claude_home() / "daemon.log"


def control_socket() -> Path:
    digest = hashlib.sha256(str(roster.claude_home()).encode()).hexdigest()[:8]
    return Path("/tmp") / f"cc-daemon-{os.getuid()}" / digest / "control.sock"


def memory_properties() -> tuple[str, ...]:
    """The unit's memory limits, from ``AWM_KEEP_MEMORY_HIGH`` and ``_MAX``."""
    out = []
    for key, prop in (("AWM_KEEP_MEMORY_HIGH", "MemoryHigh"), ("AWM_KEEP_MEMORY_MAX", "MemoryMax")):
        if value := os.environ.get(key, "").strip():
            out.append(f"{prop}={value}")
    return tuple(out)


def mamba_root() -> Path | None:
    if root := os.environ.get("MAMBA_ROOT_PREFIX"):
        return Path(root)
    prefix = Path(sys.prefix)
    if prefix.parent.name == "envs":
        return prefix.parent.parent
    return None


def daemon_env() -> dict[str, str]:
    """What the daemon needs beyond the user manager's own environment.

    The daemon hands its environment to every session it starts, so this is
    the sessions' PATH. The gateway's own environment is not copied: on a node
    where it was restarted from inside a session it carries that session's ids.
    """
    path = [str(Path.home() / ".local" / "bin")]
    env: dict[str, str] = {}
    if root := mamba_root():
        path += [str(root / "condabin"), str(root / "bin")]
        env["MAMBA_ROOT_PREFIX"] = str(root)
    path += ["/usr/local/bin", "/usr/bin", "/bin"]
    env["PATH"] = ":".join(path)
    for key in ("AWM_WORKSPACE", "CLAUDE_CONFIG_DIR"):
        if value := os.environ.get(key):
            env[key] = value
    return env


# --- what is running --------------------------------------------------------


@dataclass
class Daemon:
    pid: int
    origin: str
    unit: str | None
    started_at: float | None
    version: str | None
    control: bool
    spawned_by: str | None = None

    def uptime(self, now: float) -> float | None:
        return None if self.started_at is None else max(0.0, now - self.started_at)


def _boot_time() -> float:
    with open("/proc/stat") as fh:
        for line in fh:
            if line.startswith("btime "):
                return float(line.split()[1])
    return 0.0


def _started_at(pid: int) -> float | None:
    ticks = roster.proc_start(pid)
    if ticks is None:
        return None
    return _boot_time() + int(ticks) / os.sysconf("SC_CLK_TCK")


def _argv(pid: int) -> list[str]:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
    except OSError:
        return []


def _cgroup_unit(pid: int) -> str | None:
    try:
        text = Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("0::"):
            return line.rsplit("/", 1)[-1] or None
    return None


def _version(pid: int) -> str | None:
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return None
    return Path(exe.removesuffix(" (deleted)")).name or None


def _reachable(path: Path) -> bool:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    try:
        sock.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def describe(pid: int) -> Daemon | None:
    """What the process ``pid`` is, if it is a running Claude Code daemon."""
    argv = _argv(pid)
    if "daemon" not in argv or "run" not in argv:
        return None
    origin = argv[argv.index("--origin") + 1] if "--origin" in argv[:-1] else "unknown"
    spawned = argv[argv.index("--spawned-by") + 1] if "--spawned-by" in argv[:-1] else None
    return Daemon(pid=pid, origin=origin, unit=_cgroup_unit(pid),
                  started_at=_started_at(pid), version=_version(pid),
                  control=_reachable(control_socket()), spawned_by=spawned)


def find_daemon(main_pid: int | None = None) -> Daemon | None:
    """The running daemon: the roster's supervisor, else the unit's main process."""
    for pid in (roster.daemon_pid(), main_pid):
        if pid and (d := describe(pid)):
            return d
    return None


def parse_show(text: str) -> dict[str, Any]:
    fields = dict(line.partition("=")[::2] for line in text.splitlines() if "=" in line)
    out: dict[str, Any] = {k: fields.get(k) for k in _SHOW}
    for k in ("MainPID", "NRestarts", "ExecMainStatus"):
        try:
            out[k] = int(out[k]) if out[k] not in (None, "") else None
        except ValueError:
            out[k] = None
    return out


def history(text: str, limit: int = HISTORY) -> list[dict[str, Any]]:
    """Each daemon start in ``text`` (a daemon.log tail), with why the one
    before it stopped. A start with no shutdown line before it followed a
    SIGKILL, an OOM kill or a power loss: the daemon had no chance to log."""
    starts: list[dict[str, Any]] = []
    last_stop: dict[str, Any] | None = None
    for line in text.splitlines():
        if m := _STOP.match(line):
            last_stop = {"at": m["at"], "cause": m["cause"],
                         "uptime_s": int(m["uptime"]) if m["uptime"] else None}
        elif m := _START.match(line):
            after = last_stop or ({"cause": "killed", "at": starts[-1].get("at")} if starts else None)
            starts.append({"at": m["at"], "pid": int(m["pid"]), "version": m["version"],
                           "origin": m["origin"], "after": after,
                           "down_s": _between(after.get("at") if after else None, m["at"])})
            last_stop = None
    if last_stop:
        starts.append({"at": None, "pid": None, "version": None, "origin": None,
                       "after": last_stop})
    return starts[-limit:]


def _between(start: str | None, end: str | None) -> int | None:
    """Whole seconds between two daemon.log timestamps. For a kill the start is
    the previous start, so the figure is an upper bound."""
    from datetime import datetime
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))  # type: ignore[union-attr]
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        return None
    return max(0, round((b - a).total_seconds()))


def read_tail(path: Path, size: int = 256 * 1024) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - size))
            return fh.read().decode(errors="replace")
    except OSError:
        return ""


# --- the host, behind one seam for the tests --------------------------------


class Host:
    """Every side effect the keeper has, so a test can swap the whole host."""

    def now(self) -> float:
        return time.time()

    def bus(self) -> dict[str, str] | None:
        return launch.user_manager_env()

    def daemon(self, main_pid: int | None) -> Daemon | None:
        return find_daemon(main_pid)

    def systemctl(self, bus: dict[str, str], *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True,
                              timeout=30, env={**os.environ, **bus})

    def unit(self, bus: dict[str, str]) -> dict[str, Any]:
        args = ["show", f"{UNIT}.service", *(f"-p{k}" for k in _SHOW)]
        return parse_show(self.systemctl(bus, *args).stdout)

    def run_unit(self, bus: dict[str, str], argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(argv, capture_output=True, text=True, timeout=60,
                              env={**os.environ, **bus})

    def terminate(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def log_tail(self) -> str:
        return read_tail(daemon_log())

    def linger(self) -> bool | None:
        user = os.environ.get("USER") or _user()
        try:
            out = subprocess.run(["loginctl", "show-user", user, "-pLinger", "--value"],
                                 capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return {"yes": True, "no": False}.get(out.stdout.strip())

    def enable_linger(self) -> str | None:
        try:
            out = subprocess.run(["loginctl", "enable-linger"], capture_output=True,
                                 text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"{type(exc).__name__}: {exc}"
        return None if out.returncode == 0 else (out.stderr or out.stdout).strip()[:300]

    def held(self) -> dict[str, Any] | None:
        try:
            return json.loads((state_dir() / "hold.json").read_text())
        except (OSError, ValueError):
            return None

    def set_held(self, hold: dict[str, Any] | None) -> None:
        path = state_dir() / "hold.json"
        if hold is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(hold))


def _user() -> str:
    import pwd
    return pwd.getpwuid(os.getuid()).pw_name


def launch_argv(claude: str, env: dict[str, str], memory: tuple[str, ...]) -> list[str]:
    return ["systemd-run", "--user", "--quiet", "--collect", f"--unit={UNIT}",
            "--description=Claude Code daemon, kept by awm",
            *(f"--property={p}" for p in (*UNIT_PROPERTIES, *memory)),
            f"--working-directory={Path.home()}",
            *(f"--setenv={k}={v}" for k, v in env.items()),
            "--", claude, "daemon", "run", "--origin", "service"]


# --- the keeper -------------------------------------------------------------


@dataclass
class Keeper:
    host: Host = field(default_factory=Host)
    last_tick: float | None = None
    last: dict[str, Any] = field(default_factory=dict)
    _transient_since: float | None = None
    _last_kick: float = 0.0
    _linger_tried: bool = False
    _lock_fd: int | None = None

    def holds_lock(self) -> bool:
        """One keeper per Claude config dir, so a dev sandbox never races prod."""
        if self._lock_fd is not None:
            return True
        path = roster.claude_home() / "awm-keep.lock"
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._lock_fd = fd
        return True

    def reconcile(self, *, kick: bool = False) -> dict[str, Any]:
        """Adopt the daemon, launch it, or clear what blocks it. Called on a loop."""
        out = self._reconcile(kick=kick)
        self.last_tick = self.host.now()
        if out != self.last and out.get("action") != "none":
            log.info("keep: %s", out)
        self.last = out
        return out

    def _reconcile(self, *, kick: bool) -> dict[str, Any]:
        why = enabled()
        if why:
            return {"action": "off", "reason": why}
        if not self.holds_lock():
            return {"action": "off", "reason": "another keeper holds the lock for this Claude home"}
        if hold := self.host.held():
            return {"action": "held", "hold": hold}
        bus = self.host.bus()
        if bus is None:
            return self._no_manager()
        unit = self.host.unit(bus)
        now = self.host.now()
        d = self.host.daemon(unit.get("MainPID"))

        if d is not None and d.unit == f"{UNIT}.service":
            self._transient_since = None
            if unit.get("NRestarts") and (d.uptime(now) or 0) >= SETTLE_S:
                self.host.systemctl(bus, "reset-failed", f"{UNIT}.service")
                return {"action": "settled", "pid": d.pid, "restarts": unit["NRestarts"]}
            return {"action": "none", "pid": d.pid}
        if d is not None and d.origin != "transient":
            # A service daemon in some other unit: a hand-written one on an old
            # node. It would refuse ours anyway, so adopt it as it is.
            self._transient_since = None
            return {"action": "none", "pid": d.pid, "foreign_unit": d.unit}
        if d is not None:
            if unit.get("ActiveState") in ("active", "activating", "reloading"):
                self._transient_since = self._transient_since or now
                if now - self._transient_since < DISPLACE_S:
                    return {"action": "displacing", "pid": d.pid}
                self.host.terminate(d.pid)
                self._transient_since = None
                return {"action": "displaced", "pid": d.pid,
                        "reason": "the transient daemon did not yield to the kept one"}
            return self._launch(bus, unit, reason=f"a transient daemon (pid {d.pid}) runs")

        self._transient_since = None
        if unit.get("ActiveState") in ("active", "activating", "reloading"):
            if kick and now - self._last_kick >= KICK_EVERY_S:
                self._last_kick = now
                self.host.systemctl(bus, "start", f"{UNIT}.service")
                return {"action": "kicked", "restarts": unit.get("NRestarts")}
            return {"action": "restarting", "restarts": unit.get("NRestarts"),
                    "result": unit.get("Result")}
        return self._launch(bus, unit, reason="no daemon runs")

    def _launch(self, bus: dict[str, str], unit: dict[str, Any], *, reason: str) -> dict[str, Any]:
        memory = memory_properties()
        if unit.get("LoadState") == "loaded":
            # A dead unit whose sessions still run stays loaded, and its name
            # cannot be reused, so it is started again in place.
            self.host.systemctl(bus, "reset-failed", f"{UNIT}.service")
            if memory:
                self.host.systemctl(bus, "set-property", "--runtime", f"{UNIT}.service", *memory)
            proc = self.host.systemctl(bus, "start", "--no-block", f"{UNIT}.service")
        else:
            proc = self.host.run_unit(bus, launch_argv(claude_bin(), daemon_env(), memory))
        if proc.returncode != 0:
            return {"action": "failed", "reason": reason,
                    "error": (proc.stderr or proc.stdout or "").strip()[:400]}
        return {"action": "launched", "reason": reason}

    def _no_manager(self) -> dict[str, Any]:
        linger = self.host.linger()
        error = None
        if linger is False and not self._linger_tried:
            self._linger_tried = True
            error = self.host.enable_linger()
        return {"action": "no-manager", "linger": linger, "error": error,
                "reason": "no user systemd manager, so the daemon cannot run outside awm's "
                          "control group; run `loginctl enable-linger` for this user"}

    def ensure(self, timeout: float = ENSURE_TIMEOUT_S) -> dict[str, Any]:
        """Reconcile now and wait for a daemon that answers on its control socket."""
        deadline = time.monotonic() + timeout
        out = self.reconcile(kick=True)
        while True:
            bus = self.host.bus()
            main = self.host.unit(bus).get("MainPID") if bus else None
            d = self.host.daemon(main)
            if d is not None and d.control:
                return {"ok": True, "pid": d.pid, "origin": d.origin, "unit": d.unit,
                        "action": out.get("action")}
            if out.get("action") in ("off", "held", "no-manager", "failed") \
                    or time.monotonic() >= deadline:
                return {"ok": False, "reason": "daemon_down", **out}
            time.sleep(1.0)
            if out.get("action") == "displacing":
                out = self.reconcile()

    def hold(self, reason: str) -> dict[str, Any]:
        """Stop the daemon and keep it stopped. Its sessions keep running."""
        self.host.set_held({"since": self.host.now(), "reason": reason})
        bus = self.host.bus()
        main = None
        if bus:
            main = self.host.unit(bus).get("MainPID")
            self.host.systemctl(bus, "stop", f"{UNIT}.service")
        d = self.host.daemon(main)
        if d is not None and d.unit != f"{UNIT}.service":
            self.host.terminate(d.pid)
        return {"held": True, "reason": reason, "stopped": d.pid if d else None}

    def release(self) -> dict[str, Any]:
        self.host.set_held(None)
        return {"held": False, **self.reconcile(kick=True)}

    def status(self) -> dict[str, Any]:
        now = self.host.now()
        bus = self.host.bus()
        unit = self.host.unit(bus) if bus else None
        d = self.host.daemon(unit.get("MainPID") if unit else None)
        events = history(self.host.log_tail())
        return {
            "running": d is not None,
            "daemon": None if d is None else {**asdict(d), "uptime_s": _round(d.uptime(now))},
            "kept": d is not None and d.unit == f"{UNIT}.service",
            "unit": None if unit is None else {"name": f"{UNIT}.service", **unit},
            "manager": bus is not None,
            "linger": self.host.linger(),
            "held": self.host.held(),
            "last_stop": next((e["after"] for e in reversed(events) if e.get("after")), None),
            "history": events,
            "loop": {"enabled": enabled() or "yes", "last_tick_s": _round(
                None if self.last_tick is None else now - self.last_tick), "last": self.last},
        }


def _round(v: float | None) -> int | None:
    return None if v is None else round(v)
