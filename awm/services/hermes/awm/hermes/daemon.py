"""Supervision of the Hermes Agent dashboard — by adoption, not parenthood.

The dashboard is a FastAPI/uvicorn server that holds live chat sessions and
pseudo-terminals. ``awm gateway restart`` happens on every deploy, several
times a day, and parenting the dashboard to this service would make each of
those an interruption of somebody's session for no gain.

So this module *adopts*: on start it asks whether something is already serving
on the dashboard port and, if so, records that and leaves it alone. If nothing
is, it launches one and lets it outlive us. Stopping the service does not stop
the dashboard; ``stop`` is an explicit verb for when you mean it.

Detaching alone would not achieve that. ``systemctl restart awm`` kills by
control group, and a cgroup is inherited by every descendant however it forks —
so the launch goes through ``systemd-run --user`` into a transient unit, a
cgroup ``awm.service`` does not own. On a node with no user manager the
fallback still works, it just does not survive an awm restart, and ``status``
says so rather than leaving it to be discovered on the next deploy.

Liveness is the listening socket, not an HTTP response. A dashboard mid
inference can be slow to answer, and respawning on that would interrupt the
work this arrangement exists to protect. Nothing accepting a TCP connection on
the port is unambiguous; a slow reply is not.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("awm.hermes.daemon")

#: The hermes launcher. Written by upstream's installer into ~/.local/bin; it
#: is a wrapper that clears PYTHONPATH/PYTHONHOME before exec'ing the venv's
#: interpreter, which is why this service can hand it a gateway environment
#: carrying awm's own dist-roots without breaking its imports.
BIN = Path(os.environ.get("HERMES_BIN") or (Path.home() / ".local" / "bin" / "hermes"))

#: HERMES_HOME: config.yaml, .env, SOUL.md, sessions, skills, logs, and the
#: checkout hermes updates in place. Never ours to manage.
HOME_DIR = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))

#: Loopback port for the dashboard. Not a mesh port — the gateway mount owns
#: the public side.
PORT = int(os.environ.get("HERMES_DASHBOARD_PORT", "9119"))

#: How often the reconcile loop checks the dashboard is still listening.
HEALTH_INTERVAL_S = float(os.environ.get("HERMES_HEALTH_INTERVAL_S", "20"))

#: How long to wait for a freshly-launched dashboard to bind.
START_TIMEOUT_S = float(os.environ.get("HERMES_START_TIMEOUT_S", "180"))

#: Transient user-manager unit the dashboard is launched into.
TRANSIENT_UNIT = os.environ.get("HERMES_TRANSIENT_UNIT", "hermes-dashboard")

#: Where the SPA bundle lands (vite outDir). ``--skip-build`` needs it to
#: exist; hermes attempts one recovery build when it does not, which is why
#: the launch environment still carries a PATH with npm on it.
WEB_DIST = HOME_DIR / "hermes-agent" / "hermes_cli" / "web_dist"

#: Node lives inside HERMES_HOME because the installer refuses to trust an
#: ambient one. The recovery build is the only thing that needs it.
NODE_BIN = HOME_DIR / "node" / "bin"


def installed() -> bool:
    return BIN.is_file() and os.access(BIN, os.X_OK)


def port_listening(port: int = PORT, *, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for any hermes invocation.

    ``HERMES_HOME`` is passed explicitly on every call rather than inherited: a
    service that silently managed a *different* profile than the one it reports
    on would be worse than no service at all.
    """
    env = {**os.environ, "HOME": str(Path.home()), "HERMES_HOME": str(HOME_DIR)}
    env["PATH"] = f"{NODE_BIN}:{env.get('PATH', '')}"
    env.update(extra or {})
    return env


def _run(args: list[str], *, timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run([str(BIN), *args], capture_output=True, text=True,
                          timeout=timeout, env=_env())


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


def _systemctl(verb: str, *args: str, timeout: float = 30.0
               ) -> subprocess.CompletedProcess | None:
    bus = _user_manager_env()
    if bus is None:
        return None
    return subprocess.run(
        ["systemctl", "--user", verb, *args],
        capture_output=True, text=True, timeout=timeout, env=_env(bus),
    )


def unit_state() -> dict[str, Any]:
    """The transient unit's state, or ``{"available": False}`` with no manager.

    The difference between this being a live unit and being absent is the
    difference between surviving ``systemctl restart awm`` and not, so it
    belongs in status rather than in a log line nobody reads.
    """
    proc = _systemctl("show", f"{TRANSIENT_UNIT}.service",
                      "-p", "MainPID", "-p", "ActiveState", "-p", "SubState")
    if proc is None:
        return {"available": False, "unit": None}
    fields: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        key, _, value = line.partition("=")
        fields[key] = value
    pid = int(fields.get("MainPID") or 0) or None
    return {
        "available": True,
        "unit": f"{TRANSIENT_UNIT}.service",
        "active_state": fields.get("ActiveState"),
        "sub_state": fields.get("SubState"),
        "main_pid": pid,
    }


def dashboard_pids() -> list[int]:
    """Every live ``hermes dashboard`` server process, from /proc.

    Needed because the dashboard may have been adopted from outside our unit,
    in which case the unit's MainPID says nothing about it. Matches the server
    process itself (the venv interpreter running the hermes entry point), not
    a shell that happens to mention the words.
    """
    out: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().decode(errors="replace").split("\0")
        except OSError:
            continue
        argv = [a for a in argv if a]
        if len(argv) < 3 or "dashboard" not in argv:
            continue
        if argv[0].endswith("/python") or argv[0].endswith("/python3"):
            if any(a.endswith("/hermes") or a == "hermes_cli.main" for a in argv[1:]):
                out.append(int(entry.name))
    return sorted(out)


def health() -> dict[str, Any]:
    """The dashboard's own view of itself, or an ``error`` explaining why not.

    Never raises: this feeds ``status``, and a probe that throws turns a
    reportable problem into an unreportable one. Distinct from
    :func:`port_listening`, which is what respawn decisions are made on.
    """
    if not port_listening():
        return {"serving": False, "error": f"nothing listening on 127.0.0.1:{PORT}"}
    try:
        import httpx
        r = httpx.get(f"http://127.0.0.1:{PORT}/api/status", timeout=10)
    except Exception as exc:  # noqa: BLE001 — a probe reports, it does not raise
        return {"serving": True, "error": f"{type(exc).__name__}: {exc}"}
    if r.status_code != 200:
        return {"serving": True, "error": f"HTTP {r.status_code} from /api/status"}
    try:
        return {"serving": True, "api_status": r.json()}
    except json.JSONDecodeError:
        return {"serving": True, "error": "unparseable /api/status body"}


def model_config() -> dict[str, Any]:
    """``model:`` from config.yaml, as hermes itself resolves it."""
    if not installed():
        return {"error": f"hermes is not installed at {BIN}"}
    try:
        proc = _run(["config", "get", "model"], timeout=60.0)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    out: dict[str, Any] = {}
    for line in (proc.stdout or "").splitlines():
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip()
    return out or {"error": (proc.stderr or proc.stdout or "").strip()[:200]}


def set_model(model: str) -> dict[str, Any]:
    proc = _run(["config", "set", "model.default", model], timeout=60.0)
    if proc.returncode != 0:
        raise RuntimeError(
            f"hermes config set failed ({proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()[:300]}"
        )
    return model_config()


class Supervisor:
    """Adopt-or-start lifecycle for the dashboard.

    Stateless with respect to the dashboard: every question is answered by
    probing, so a gateway restart that loses this object's memory changes
    nothing about what it reports.
    """

    def __init__(self) -> None:
        #: Whether the dashboard we report on was already up when we arrived.
        #: "adopted" and "we started it" fail in different ways, and only one
        #: of them implicates this service.
        self.adopted: bool | None = None
        self.started_at: float | None = None
        self.restarts = 0

    # -- state ------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        unit = unit_state()
        pids = dashboard_pids()
        return {
            "listening": port_listening(),
            "port": PORT,
            "pids": pids,
            "pid": unit.get("main_pid") or (pids[0] if pids else None),
            "home": str(HOME_DIR),
            "bin": str(BIN),
            "installed": installed(),
            "web_dist": WEB_DIST.is_dir(),
            # A null unit means this node has no user manager and the dashboard
            # will NOT outlive `systemctl restart awm`.
            "user_unit": unit,
            "adopted": self.adopted,
            "restarts_by_us": self.restarts,
            "supervised_since": self.started_at,
        }

    # -- lifecycle --------------------------------------------------------

    def start(self) -> dict[str, Any]:
        """Adopt a serving dashboard, or launch one. Idempotent."""
        if not installed():
            raise RuntimeError(f"hermes is not installed at {BIN} — run install.sh")
        if port_listening():
            if self.adopted is None:
                self.adopted = True
                log.info("hermes: adopted dashboard already serving on :%d", PORT)
            return self.snapshot()

        serve = [str(BIN), "dashboard", "--no-open", "--skip-build",
                 "--host", "127.0.0.1", "--port", str(PORT)]
        bus = _user_manager_env()
        if bus:
            # A unit left over from a dashboard that has already exited — or
            # one still deactivating from the `stop` half of a restart — would
            # make systemd-run refuse the name. Clearing it is routine, not an
            # error path: both verbs are no-ops when there is nothing there.
            for verb in ("stop", "reset-failed"):
                _systemctl(verb, TRANSIENT_UNIT)
            launch = [
                "systemd-run", "--user", "--quiet", "--collect",
                f"--unit={TRANSIENT_UNIT}", "--property=Restart=no",
                f"--setenv=HERMES_HOME={HOME_DIR}",
                f"--setenv=PATH={NODE_BIN}:{os.environ.get('PATH', '')}",
                "--", *serve,
            ]
            how = f"into user unit {TRANSIENT_UNIT}.service"
        else:
            # No user manager (a container, a dev box). The dashboard still
            # runs; it just shares awm's cgroup and dies with it. `status`
            # reports the null unit so this is visible before the next deploy.
            launch = serve
            how = "unsupervised (no user systemd manager)"

        log.info("hermes: launching dashboard %s on 127.0.0.1:%d", how, PORT)
        if bus:
            proc = subprocess.run(launch, capture_output=True, text=True,
                                  timeout=60.0, env=_env(bus))
            if proc.returncode != 0:
                raise RuntimeError(
                    f"systemd-run failed ({proc.returncode}): "
                    f"{(proc.stderr or proc.stdout or '').strip()[:400]}"
                )
        else:
            subprocess.Popen(launch, env=_env(), start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # The launch returns as soon as systemd has queued the job; the bind is
        # what we actually depend on, so make the bind decide we succeeded.
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if port_listening():
                self.adopted = False
                self.started_at = time.time()
                return self.snapshot()
            time.sleep(1.0)
        raise RuntimeError(
            f"hermes dashboard did not bind :{PORT} within {START_TIMEOUT_S:.0f}s"
        )

    def stop(self) -> dict[str, Any]:
        """Stop the dashboard, ending live chat sessions and PTYs. Idempotent.

        Stopping the *service* does not do this; this verb is for when you
        mean it.
        """
        if _systemctl("stop", TRANSIENT_UNIT, timeout=60.0) is None:
            # No user manager: nothing owns a unit, so signal the processes.
            for pid in dashboard_pids():
                try:
                    os.kill(pid, 15)
                except OSError:
                    pass
        # A dashboard adopted from outside our unit is not in it, so the unit
        # stop above leaves it running. Signal what is left.
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and port_listening():
            for pid in dashboard_pids():
                try:
                    os.kill(pid, 15)
                except OSError:
                    pass
            time.sleep(1.0)
        self.adopted = None
        self.started_at = None
        return self.snapshot()

    def restart(self) -> dict[str, Any]:
        self.stop()
        snap = self.start()
        self.restarts += 1
        return snap

    def reconcile(self) -> dict[str, Any]:
        """One pass of the supervision loop: relaunch a dashboard that has died.

        Decides on the listening socket alone. A dashboard mid inference can be
        slow to answer HTTP, and treating that as death would interrupt the
        session this service exists to protect.
        """
        if not installed():
            return {"action": "skipped", "reason": "hermes not installed"}
        if port_listening():
            return {"action": "ok"}
        log.warning("hermes: nothing listening on :%d — starting the dashboard",
                    PORT)
        self.start()
        self.restarts += 1
        return {"action": "respawned"}

    # -- one-shot queries -------------------------------------------------

    def logs(self, tail: int = 200) -> dict[str, Any]:
        """The dashboard's output, from the journal when we own the unit.

        A dashboard we adopted writes nowhere we can reach, so fall back to
        hermes' own log files and say which source answered — an empty tail
        from the wrong source reads as "nothing happened" when it is not.
        """
        tail = max(1, int(tail))
        bus = _user_manager_env()
        if bus and shutil.which("journalctl"):
            proc = subprocess.run(
                ["journalctl", "--user", "-u", f"{TRANSIENT_UNIT}.service",
                 "-n", str(tail), "--no-pager"],
                capture_output=True, text=True, timeout=30.0, env=_env(bus),
            )
            text = (proc.stdout or "").strip()
            if text and "No entries" not in text:
                return {"source": f"journal:{TRANSIENT_UNIT}.service", "log": text}
        parts = []
        for name in ("errors.log", "agent.log"):
            path = HOME_DIR / "logs" / name
            if path.is_file():
                lines = path.read_text(errors="replace").splitlines()[-tail:]
                parts.append(f"--- {path} ---\n" + "\n".join(lines))
        return {"source": str(HOME_DIR / "logs"),
                "log": "\n\n".join(parts) or "no log output found"}
