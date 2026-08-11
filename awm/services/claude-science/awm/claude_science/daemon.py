"""Supervision of the Claude Science workbench daemon — by adoption, not parenthood.

Every other awm service owns its child: it spawns the process, sets
``PR_SET_PDEATHSIG`` so the child cannot outlive it, and the gateway's
supervision therefore covers the whole stack in one lifetime. That is the right
default, and it is the wrong one here.

The workbench is not a stateless renderer. It holds live conversations, a
sandbox with bind mounts asserted into it, and analysis runs that can last
minutes; its data directory carries a ~6 GB conda tree that takes real time to
warm. An ``awm gateway restart`` happens on every deploy, several times a day.
Parenting the daemon to this process would make each of those deploys an
interruption of somebody's work, for no gain — the daemon already has its own
watchdog and its own crash recovery.

So: this module *adopts*. On start it asks the binary whether a daemon is
already running and, if one is, records the fact and leaves it alone. If none
is, it launches one detached and lets it outlive us. Stopping the service does
not stop the workbench; ``stop`` is an explicit verb for when you mean it.

What that buys, versus the hand-rolled systemd unit this replaces: the daemon
is visible in ``awm services list``, its health is a verb rather than a
``systemctl status``, and starting/stopping/restarting it is reachable from any
node in the fleet.

The single source of truth for liveness is ``claude-science status``, which
emits JSON, always exits 0, and reports ``running``/``pid``/``version``/``port``.
It goes through the daemon socket in the data dir, so it sees the daemon that
actually holds the lock rather than whatever this process happens to remember.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger("awm.claude_science.daemon")

HERE = Path(__file__).resolve().parent          # …/claude-science/awm/claude_science
SERVICE_DIR = HERE.parents[1]                   # …/awm/services/claude-science

#: The workbench binary. Installed by upstream's own installer into ~/.local/bin;
#: this service never builds it (see INSTALL.md § Install).
BIN = Path(os.environ.get("CLAUDE_SCIENCE_BIN")
           or (Path.home() / ".local" / "bin" / "claude-science"))

#: The workbench's data directory: conversations, artifacts, OAuth tokens, the
#: encryption key, and the provisioned conda/R environments. Never ours to
#: manage — we only report on whether it has been provisioned yet.
DATA_DIR = Path(os.environ.get("CLAUDE_SCIENCE_DATA_DIR")
                or (Path.home() / ".claude-science"))

#: Loopback port for the workbench UI. Not the mesh port — the front owns that.
PORT = int(os.environ.get("CLAUDE_SCIENCE_UPSTREAM_PORT", "12203"))

#: Loopback port for generated-HTML previews. Upstream keeps this on a separate
#: port on purpose: a previewed page must not be same-origin with the session
#: that generated it. We preserve that separation all the way out to the mesh.
SANDBOX_PORT = int(os.environ.get("CLAUDE_SCIENCE_SANDBOX_PORT", "12204"))

#: How often the reconcile loop checks that the daemon is still alive.
HEALTH_INTERVAL_S = float(os.environ.get("CLAUDE_SCIENCE_HEALTH_INTERVAL_S", "20"))

#: How long to wait for a freshly-launched daemon to report itself running.
#: First launch provisions ~6 GB of environments, but it binds and answers
#: `status` long before that finishes, so this covers the bind, not the setup.
START_TIMEOUT_S = float(os.environ.get("CLAUDE_SCIENCE_START_TIMEOUT_S", "90"))

#: Sentinel names inside the data dir that mean "first-run provisioning has
#: happened". Checked rather than reproduced: the daemon owns this work.
_PROVISION_MARKERS = ("conda", "seed-assets")

#: Transient user-manager unit the daemon is launched into where one is
#: available. See :func:`_user_manager_env` for why it is not simply spawned.
TRANSIENT_UNIT = os.environ.get("CLAUDE_SCIENCE_TRANSIENT_UNIT",
                                "claude-science-daemon")


def installed() -> bool:
    return BIN.is_file() and os.access(BIN, os.X_OK)


def provisioned() -> bool:
    return DATA_DIR.is_dir() and all(
        (DATA_DIR / marker).exists() for marker in _PROVISION_MARKERS
    )


def _run(args: list[str], *, timeout: float = 60.0) -> subprocess.CompletedProcess:
    """Invoke the binary with our data dir, never inheriting a stray one.

    ``--data-dir`` is passed explicitly on every call rather than relying on the
    default: a service that silently managed a *different* instance than the one
    it reports on would be worse than no service at all.
    """
    return subprocess.run(
        [str(BIN), *args, "--data-dir", str(DATA_DIR)],
        capture_output=True, text=True, timeout=timeout,
        # A minimal, explicit environment: this runs under the gateway, whose
        # own env is not the interactive shell's.
        env={**os.environ, "HOME": str(Path.home())},
    )


def status_json() -> dict[str, Any]:
    """``claude-science status`` as a dict, or an ``error`` key explaining why not.

    Never raises: this is the health probe, and a probe that throws turns a
    reportable problem into an unreportable one.
    """
    if not installed():
        return {"running": False, "error": f"binary not installed at {BIN}"}
    try:
        proc = _run(["status"], timeout=30.0)
    except subprocess.TimeoutExpired:
        return {"running": False, "error": "status timed out after 30s"}
    except OSError as exc:
        return {"running": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        head = (proc.stdout or proc.stderr or "").strip()[:200]
        return {"running": False, "error": f"unparseable status output: {head!r}"}


def _origin_host() -> str:
    """The host a browser reaches this node at.

    Deliberately declared, not enumerated. On the production node awm runs
    inside WSL and the mesh address belongs to the *Windows* host, which is
    invisible from in here — the same reason ``AWM_EDGE_URL`` and ``.sans``
    exist. Falls back to loopback so a dev box works without configuration.
    """
    explicit = os.environ.get("CLAUDE_SCIENCE_PUBLIC_HOST")
    if explicit:
        return explicit
    edge = os.environ.get("AWM_EDGE_URL")
    if edge:
        host = urlsplit(edge).hostname
        if host:
            return host
    return "127.0.0.1"


def front_origin(port: int) -> str:
    return f"https://{_origin_host()}:{port}"


def _user_manager_env() -> dict[str, str] | None:
    """Env for reaching this user's systemd manager, or ``None`` if there is none.

    Detaching is not enough to make the daemon outlive an awm restart, and this
    is the correction to that assumption. ``serve --detached`` already puts the
    daemon in its own session with ``PPID 1``, which defeats every *signal*-
    based teardown — but on a node where awm runs under systemd, ``systemctl
    restart awm`` kills by **cgroup** (``KillMode=control-group``, the default),
    and a cgroup is inherited by every descendant no matter how it detaches. So
    a daemon we spawned died on the deploy action it was supposed to survive,
    while the daemon we *adopted* from its own unit sailed through — the bug was
    invisible until this service became the one doing the spawning.

    Launching into a transient unit of the *user* manager puts the daemon in a
    cgroup awm.service does not own, which is the actual property wanted. The
    unit is transient, so it does not survive a reboot — after one, this service
    starts a fresh daemon exactly as it would on any cold node.
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


def allowed_origins(main_front_port: int) -> list[str]:
    """Exact origins the daemon accepts WebSocket upgrades and writes from.

    Matched exactly on scheme+host+port, so every address a browser might use
    has to be listed. ``CLAUDE_SCIENCE_EXTRA_ORIGINS`` is how the second one
    gets in on a WSL host, where the app is reachable both at the Windows
    ZeroTier address and at the WSL interface's own address.
    """
    origins = [front_origin(main_front_port)]
    extra = os.environ.get("CLAUDE_SCIENCE_EXTRA_ORIGINS", "")
    for raw in extra.split(","):
        origin = raw.strip().rstrip("/")
        if origin and origin not in origins:
            origins.append(origin)
    return origins


class Supervisor:
    """Adopt-or-start lifecycle for the workbench daemon.

    Stateless with respect to the daemon: every question is answered by asking
    the binary, so a gateway restart that loses this object's memory changes
    nothing about what it reports.
    """

    def __init__(self, *, main_front_port: int, sandbox_front_port: int) -> None:
        self.main_front_port = main_front_port
        self.sandbox_front_port = sandbox_front_port
        #: Whether the daemon we are reporting on was already up when we
        #: arrived. Worth surfacing: "adopted" and "we started it" fail in
        #: different ways, and only one of them implicates this service.
        self.adopted: bool | None = None
        self.started_at: float | None = None
        self.restarts = 0

    # -- state ------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        st = status_json()
        return {
            "running": bool(st.get("running")),
            "pid": st.get("pid"),
            "version": st.get("version"),
            "port": st.get("port"),
            "sandbox_port": SANDBOX_PORT,
            # Which cgroup the daemon would be launched into. The difference
            # between this being a unit name and being null is the difference
            # between surviving `systemctl restart awm` and not, so it belongs
            # in status rather than in a log line nobody reads.
            "user_unit": (f"{TRANSIENT_UNIT}.service"
                          if _user_manager_env() else None),
            "adopted": self.adopted,
            "restarts_by_us": self.restarts,
            "supervised_since": self.started_at,
            "error": st.get("error"),
            "daemon": st.get("daemon"),
        }

    def install_state(self) -> dict[str, Any]:
        """Which of the three install steps are done — named separately so a
        fresh node's failure explains itself instead of just being 'down'."""
        return {
            "binary": {"path": str(BIN), "installed": installed()},
            "data_dir": {"path": str(DATA_DIR), "provisioned": provisioned()},
            "daemon": {"running": bool(status_json().get("running"))},
        }

    # -- lifecycle --------------------------------------------------------

    def start(self) -> dict[str, Any]:
        """Adopt a running daemon, or launch one detached. Idempotent."""
        if not installed():
            raise RuntimeError(
                f"claude-science is not installed at {BIN} — run install.sh"
            )
        st = status_json()
        if st.get("running"):
            if self.adopted is None:
                self.adopted = True
                log.info("claude-science: adopted running daemon pid=%s version=%s",
                         st.get("pid"), st.get("version"))
            return self.snapshot()

        origins = allowed_origins(self.main_front_port)
        args = [
            "serve", "--no-browser",
            "--host", "127.0.0.1",
            "--port", str(PORT),
            "--sandbox-port", str(SANDBOX_PORT),
        ]
        for origin in origins:
            args += ["--allow-origin", origin]

        env_note = {
            # The preview origin the daemon hands to generated pages. Declared
            # for the same reason as the allow-origins: it is a browser-facing
            # address this process cannot see.
            "OPERON_SANDBOX_ORIGIN": front_origin(self.sandbox_front_port),
        }
        serve = [str(BIN), *args, "--data-dir", str(DATA_DIR)]
        bus = _user_manager_env()
        if bus:
            # A unit left over from a daemon that has already exited — or one
            # still deactivating from the `stop` half of a restart — would make
            # systemd-run refuse the name. Clearing it is routine, not an error
            # path: `stop` blocks until the job finishes, `reset-failed` clears
            # a failed corpse, and both are no-ops when there is nothing there.
            for verb in ("stop", "reset-failed"):
                subprocess.run(
                    ["systemctl", "--user", verb, TRANSIENT_UNIT],
                    capture_output=True, text=True, timeout=30,
                    env={**os.environ, **bus},
                )
            launch = [
                "systemd-run", "--user", "--quiet", "--collect",
                f"--unit={TRANSIENT_UNIT}", "--property=Restart=no",
                *(f"--setenv={k}={v}" for k, v in env_note.items()),
                "--", *serve,
            ]
            how = f"into user unit {TRANSIENT_UNIT}.service"
        else:
            # No user manager (a container, a dev box): fall back to the
            # binary's own detach. It still survives signals, just not a
            # cgroup-scoped kill of whatever launched us.
            launch = [str(BIN), *args, "--detached", "--data-dir", str(DATA_DIR)]
            how = "detached (no user systemd manager)"

        log.info("claude-science: launching daemon %s on :%d (preview :%d) "
                 "origins=%s", how, PORT, SANDBOX_PORT, origins)
        proc = subprocess.run(
            launch, capture_output=True, text=True, timeout=START_TIMEOUT_S,
            env={**os.environ, **env_note, **(bus or {}),
                 "HOME": str(Path.home())},
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"claude-science serve failed ({proc.returncode}): "
                f"{(proc.stderr or proc.stdout or '').strip()[:400]}"
            )

        # `serve --detached` returns once the daemon is up, but poll anyway:
        # the contract we depend on is `status`, so make `status` the thing that
        # decides we succeeded.
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if status_json().get("running"):
                self.adopted = False
                self.started_at = time.time()
                return self.snapshot()
            time.sleep(1.0)
        raise RuntimeError(
            f"claude-science did not report running within {START_TIMEOUT_S:.0f}s"
        )

    def stop(self) -> dict[str, Any]:
        """Stop the daemon through its own socket. Idempotent.

        Never a bare kill: the daemon holds an owner lock, a unix socket and
        sandbox binds in the data dir, and tearing the process out from under
        those leaves them for the next start to trip over.
        """
        if not installed():
            return {"running": False, "error": f"binary not installed at {BIN}"}
        if not status_json().get("running"):
            return self.snapshot()
        proc = _run(["stop"], timeout=120.0)
        if proc.returncode != 0:
            log.warning("claude-science stop returned %d: %s", proc.returncode,
                        (proc.stderr or "").strip()[:200])
        self.adopted = None
        self.started_at = None
        return self.snapshot()

    def restart(self) -> dict[str, Any]:
        self.stop()
        snap = self.start()
        self.restarts += 1
        return snap

    def reconcile(self) -> dict[str, Any]:
        """One pass of the supervision loop: restart a daemon that has died.

        Watches liveness, not responsiveness. A slow HTTP probe on a box mid
        analysis is not evidence of death, and restarting on it would turn a
        busy minute into an interrupted conversation — the exact failure this
        service exists to avoid.
        """
        if not installed():
            return {"action": "skipped", "reason": "binary not installed"}
        if status_json().get("running"):
            return {"action": "ok"}
        # Only respawn something we are allowed to own. A daemon that was
        # adopted and has since exited was somebody else's to stop — but it is
        # also now nobody's, so bringing it back is the useful behaviour.
        log.warning("claude-science: daemon is not running — starting it")
        self.start()
        self.restarts += 1
        return {"action": "respawned"}

    # -- one-shot queries -------------------------------------------------

    def logs(self, tail: int = 200) -> str:
        if not installed():
            return f"binary not installed at {BIN}"
        proc = _run(["logs", "--tail", str(int(tail))], timeout=30.0)
        return (proc.stdout or proc.stderr or "").strip()

    def login_url(self) -> str:
        """A fresh single-use sign-in link, as printed by the binary.

        Loopback-shaped (``http://127.0.0.1:<port>/?nonce=…``) because that is
        what the daemon prints; it is useful over SSH as-is, and the front's
        sign-in route uses the nonce rather than the URL.
        """
        proc = _run(["url"], timeout=30.0)
        out = (proc.stdout or "").strip()
        if not out:
            raise RuntimeError(
                f"claude-science url printed nothing: "
                f"{(proc.stderr or '').strip()[:200]}"
            )
        return out.splitlines()[-1].strip()

    def update_check(self) -> dict[str, Any]:
        proc = _run(["update", "--check"], timeout=120.0)
        return {
            "installed_version": status_json().get("version"),
            "output": (proc.stdout or proc.stderr or "").strip(),
            "exit_code": proc.returncode,
        }


def which_binary() -> str | None:
    """Where a `claude-science` on PATH lives, for diagnosing a mismatch with
    the configured one."""
    found = shutil.which("claude-science")
    return found or None
