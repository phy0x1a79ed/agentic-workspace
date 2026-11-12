"""Supervision of the Claude Science workbench daemon — on demand, by adoption, not parenthood.

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
already running and, if one is, records the fact and leaves it alone. It never
launches one by itself: the daemon starts on the first request (a browser hit
through auth's fronts, or a verb that needs it) and ``reconcile`` stops it
after ``IDLE_S`` with no request and no conversation in flight. A daemon that
dies stays down until the next request. A launched daemon is detached and
outlives us. Stopping the service does not stop the workbench; ``stop`` is an
explicit verb for when you mean it.

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
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from awm.gatewayclient.proc import user_manager_env

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

#: Loopback port for the workbench UI. The ``[[service.front]]`` tables in
#: ``awm-plugin.toml`` name this port, the preview port and both front ports too.
PORT = 12203

#: Loopback port for generated-HTML previews. Upstream keeps this on a separate
#: port on purpose: a previewed page must not be same-origin with the session
#: that generated it. We preserve that separation all the way out to the mesh.
SANDBOX_PORT = 12204

FRONT_PORT = 12211
SANDBOX_FRONT_PORT = 12212

#: How often the reconcile loop checks that the daemon is still alive.
HEALTH_INTERVAL_S = float(os.environ.get("CLAUDE_SCIENCE_HEALTH_INTERVAL_S", "20"))

#: Seconds with no request before the daemon is stopped.
IDLE_S = float(os.environ.get("CLAUDE_SCIENCE_IDLE_S", "7200"))

#: How long to wait for a freshly-launched daemon to report itself running.
#: First launch provisions ~6 GB of environments, but it binds and answers
#: `status` long before that finishes, so this covers the bind, not the setup.
START_TIMEOUT_S = float(os.environ.get("CLAUDE_SCIENCE_START_TIMEOUT_S", "90"))

#: How long to keep asking a running daemon for a sign-in nonce. The daemon
#: answers `status` within seconds of launch but cannot mint until its boot
#: finishes, which took two minutes on altair's slow disk.
STOP_GRACE_S = 10
MINT_TIMEOUT_S = float(os.environ.get("CLAUDE_SCIENCE_MINT_TIMEOUT_S", "180"))

#: Sentinel names inside the data dir that mean "first-run provisioning has
#: happened". Checked rather than reproduced: the daemon owns this work.
_PROVISION_MARKERS = ("conda", "seed-assets")

#: Transient user-manager unit the daemon is launched into where one is
#: available. See :func:`user_manager_env` for why it is not simply spawned.
TRANSIENT_UNIT = os.environ.get("CLAUDE_SCIENCE_TRANSIENT_UNIT",
                                "claude-science-daemon")


#: The daemon's memory of which grants its credential scan judges, keyed by
#: inode. A record outlives its grant, so a folder once granted rw stays judged
#: until its record is dropped while the daemon is down.
GRANT_SCANS = DATA_DIR / "sandbox-grant-scans.json"


def prune_grant_scans(path: Path, keep: set[str]) -> list[str]:
    """Drop the scan records whose grant is not in ``keep``; return their paths.

    Only while the daemon is down: a running one would write its copy back.
    """
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    records = doc.get("records") or {}
    kept = {k: r for k, r in records.items() if r.get("grant") in keep}
    if len(kept) == len(records):
        return []
    doc["records"] = kept
    tmp = path.with_name(path.name + ".tmp")
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
              "w") as f:
        os.fchmod(f.fileno(), 0o600)
        json.dump(doc, f)
    os.replace(tmp, path)
    return [r.get("path") for k, r in records.items() if k not in kept]


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


def port_listening(port: int, *, timeout: float = 1.0) -> bool:
    """True if anything accepts a loopback TCP connection on ``port``.

    Corroboration for :func:`status_json`, not a replacement. It answers only
    "is something serving there", which is precisely the question worth asking
    before killing a daemon: a listener that accepts connections is not a
    process that has died, whatever the probe just said.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def launch_flags(pid: int | None) -> list[str] | None:
    """The flags the running daemon was actually launched with, from ``/proc``.

    Its argv is built once, at launch, and this service adopts a daemon rather
    than relaunching it — so what we would pass today and what the live daemon
    is running diverge for as long as an old one stays adopted. That gap is
    invisible in ``claude-science status``, which reports the daemon's own view
    and says nothing about its flags. Reading argv answers it locally, with no
    nonce exchange against the daemon's API.
    """
    if not pid:
        return None
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return [arg for arg in raw.decode(errors="replace").split("\0")
            if arg.startswith("--")]


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
    """Adopt, start on demand and stop when idle: the workbench daemon's lifecycle.

    Stateless with respect to the daemon: every question is answered by asking
    the binary, so a gateway restart that loses this object's memory changes
    nothing about what it reports. The one thing it remembers is when the last
    request arrived, which a restart resets to the moment the daemon is adopted.
    """

    def __init__(self, *, main_front_port: int, sandbox_front_port: int) -> None:
        self.main_front_port = main_front_port
        self.sandbox_front_port = sandbox_front_port
        #: Whether the daemon we are reporting on was already up when we
        #: arrived. Worth surfacing: "adopted" and "we started it" fail in
        #: different ways, and only one of them implicates this service.
        self.adopted: bool | None = None
        self.started_at: float | None = None
        #: Wall time of the last request, or of adoption. ``None`` while no daemon is known.
        self.last_activity: float | None = None
        self._lifecycle = threading.RLock()
        self._waking = threading.Lock()

    # -- state ------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        st = status_json()
        flags = launch_flags(st.get("pid"))
        running = bool(st.get("running"))
        if running:
            state = "running"
        elif not installed():
            state = "not-installed"
        elif st.get("error"):
            state = "unknown"
        else:
            # Stopped for want of requests: the normal resting state, not a fault.
            state = "idle-stopped"
        return {
            "state": state,
            "running": running,
            "pid": st.get("pid"),
            # Read off the live daemon's argv, not off our config: an adopted
            # daemon predating a flag change is still running the old one, and
            # only a restart moves it.
            "skip_approvals": (None if flags is None
                               else "--dangerously-skip-approvals" in flags),
            "version": st.get("version"),
            "port": st.get("port"),
            "sandbox_port": SANDBOX_PORT,
            # Which cgroup the daemon would be launched into. The difference
            # between this being a unit name and being null is the difference
            # between surviving `systemctl restart awm` and not, so it belongs
            # in status rather than in a log line nobody reads.
            "user_unit": (f"{TRANSIENT_UNIT}.service"
                          if user_manager_env() else None),
            "adopted": self.adopted,
            "last_activity": self.last_activity,
            "idle_limit_s": IDLE_S,
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

    def touch(self) -> None:
        """Count a request: the idle clock restarts."""
        self.last_activity = time.time()

    def adopt(self) -> dict[str, Any]:
        """Adopt a daemon that is already running, and launch nothing."""
        if installed():
            st = status_json()
            if st.get("running"):
                with self._lifecycle:
                    if self.adopted is None:
                        self.adopted = True
                        log.info("claude-science: adopted running daemon pid=%s version=%s",
                                 st.get("pid"), st.get("version"))
                    if self.last_activity is None:
                        self.touch()
        return self.snapshot()

    def ensure_running(self) -> dict[str, Any]:
        """Count a request and return once the daemon is up, starting it if it is down."""
        self.touch()
        return self.start()

    def wake(self) -> bool:
        """Count a request and start a stopped daemon off the caller's thread.

        False when a start is already under way, so two wakes at once start one daemon.
        """
        self.touch()
        if not self._waking.acquire(blocking=False):
            return False

        def run() -> None:
            try:
                self.start()
            except Exception:  # noqa: BLE001 — the next request wakes it again
                log.exception("claude-science: starting the daemon on demand failed")
            finally:
                self._waking.release()

        threading.Thread(target=run, name="claude-science-wake", daemon=True).start()
        return True

    def start(self) -> dict[str, Any]:
        """Adopt a running daemon, or launch one detached. Idempotent."""
        with self._lifecycle:
            return self._start()

    def _start(self) -> dict[str, Any]:
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
            if self.last_activity is None:
                self.touch()
            return self.snapshot()

        origins = allowed_origins(self.main_front_port)
        args = [
            "serve", "--no-browser",
            "--host", "127.0.0.1",
            "--port", str(PORT),
            "--sandbox-port", str(SANDBOX_PORT),
            # Every workbench awm supervises runs unattended, so an approval
            # card is a stall, not a check: nobody is watching the UI to click
            # it. This is *not* `--dangerously-no-sandbox` — bubblewrap, the
            # egress proxy and the host grants all stay exactly as they were,
            # and they are the actual boundary. What goes away is the prompt in
            # front of them, which was only ever load-bearing for a human at a
            # browser. Note what it does not buy: the sandbox still refuses
            # every private and loopback address outright ("cannot be granted"
            # — it is not an approval the flag can auto-grant), so this does
            # nothing to bring awm's own gateway within reach.
            "--dangerously-skip-approvals",
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
        bus = user_manager_env()
        if bus:
            # A unit left over from a daemon that has already exited — or one
            # still deactivating from the `stop` half of a restart — would make
            # systemd-run refuse the name. Clearing it is routine, not an error
            # path: `stop` blocks until the job finishes, `reset-failed` clears
            # a failed corpse, and both are no-ops when there is nothing there.
            for verb in ("stop", "reset-failed"):
                subprocess.run(
                    ["systemctl", "--user", verb, TRANSIENT_UNIT],
                    capture_output=True, text=True, timeout=STOP_GRACE_S + 15,
                    env={**os.environ, **bus},
                )
            launch = [
                "systemd-run", "--user", "--quiet", "--collect",
                f"--unit={TRANSIENT_UNIT}", "--property=Restart=no",
                # The daemon ignores SIGTERM, so systemd's default 90 s grace
                # outlasts every caller's timeout before the SIGKILL lands.
                f"--property=TimeoutStopSec={STOP_GRACE_S}",
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
                self.touch()
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
        with self._lifecycle:
            if not status_json().get("running"):
                return self.snapshot()
            keep = self._rw_grants()
            proc = _run(["stop"], timeout=120.0)
            if proc.returncode != 0:
                log.warning("claude-science stop returned %d: %s", proc.returncode,
                            (proc.stderr or "").strip()[:200])
            self.adopted = None
            self.started_at = None
            self.last_activity = None
            snap = self.snapshot()
            if keep is not None and not snap["running"]:
                try:
                    dropped = prune_grant_scans(GRANT_SCANS, keep)
                except (OSError, ValueError) as exc:
                    log.warning("claude-science: grant scans not pruned: %s", exc)
                else:
                    if dropped:
                        log.info("claude-science: dropped stale grant scans %s",
                                 dropped)
            return snap

    def _rw_grants(self) -> set[str] | None:
        """The live rw grants' paths, or None when the daemon cannot say."""
        from awm.claude_science import api  # api imports this module
        try:
            with api.connect(self.login_url()) as owner:
                grants = api.list_grants(owner)
        except Exception as exc:  # noqa: BLE001 — pruning is best effort
            log.warning("claude-science: could not list grants (%s); "
                        "not pruning grant scans", exc)
            return None
        return {p for g in grants if g.get("mode") == "rw"
                for p in (g.get("hostPath"), g.get("id")) if p}

    def restart(self) -> dict[str, Any]:
        with self._lifecycle:
            self.stop()
            return self.start()

    def reconcile(self, busy: Callable[[], Any] | None = None) -> dict[str, Any]:
        """One pass of the supervision loop: stop a daemon idle past ``IDLE_S``.

        Never starts one: a daemon that is down stays down until a request wakes it.
        ``busy`` returns the work in flight (empty when none); a daemon with work
        running is not stopped, and the idle clock restarts when that work ends.

        Watches liveness, not responsiveness. A slow HTTP probe on a box mid
        analysis is not evidence of death, and acting on it would turn a busy
        minute into an interrupted conversation — the exact failure this service
        exists to avoid.
        """
        if not installed():
            return {"action": "skipped", "reason": "binary not installed"}
        st = status_json()

        # `status_json` never raises, so a timed-out subprocess, an OSError and
        # unparseable output all arrive here indistinguishable from a daemon
        # that is genuinely down — running=False, plus an `error`. Those are
        # "could not find out", and stopping on them is destructive: an
        # inconclusive probe once took down a healthy workbench and its live
        # conversation, while the daemon was still answering HTTP 200s on either
        # side of the tick. Wait for a probe that actually knows.
        if st.get("error"):
            log.warning("claude-science: liveness probe inconclusive (%s); "
                        "leaving the daemon alone", st["error"])
            return {"action": "skipped",
                    "reason": f"probe inconclusive: {st['error']}"}

        if not st.get("running"):
            # A clean not-running that the socket contradicts is not evidence
            # either: something is serving on that port. Say so loudly; a
            # persistent disagreement is a real fault.
            if port_listening(PORT):
                log.warning("claude-science: status reports not-running but :%d "
                            "still accepts connections", PORT)
                return {"action": "skipped", "reason": "port still listening"}
            self.last_activity = None
            return {"action": "down"}

        with self._lifecycle:
            if self.last_activity is None:
                self.touch()
            idle = time.time() - self.last_activity
            if idle < IDLE_S:
                return {"action": "ok", "idle_s": idle}
            if busy is not None:
                try:
                    work = busy()
                except Exception as exc:  # noqa: BLE001 — no evidence, no stop
                    log.warning("claude-science: could not ask the workbench what "
                                "is running (%s); not stopping it", exc)
                    return {"action": "skipped", "reason": f"busy check failed: {exc}"}
                if work:
                    self.touch()
                    return {"action": "ok", "reason": "work running"}
            log.info("claude-science: no request for %.0fs — stopping the daemon", idle)
            self.stop()
        return {"action": "stopped", "idle_s": idle}

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
        sign-in route uses the nonce rather than the URL. A daemon still booting
        cannot mint yet, so this retries until ``MINT_TIMEOUT_S``.
        """
        deadline = time.monotonic() + MINT_TIMEOUT_S
        while True:
            proc = _run(["url"], timeout=30.0)
            out = (proc.stdout or "").strip()
            if out:
                return out.splitlines()[-1].strip()
            if time.monotonic() >= deadline or not status_json().get("running"):
                raise RuntimeError(
                    f"claude-science url printed nothing: "
                    f"{(proc.stderr or '').strip()[:200]}"
                )
            time.sleep(3.0)

    def update_check(self) -> dict[str, Any]:
        proc = _run(["update", "--check"], timeout=120.0)
        return {
            "installed_version": status_json().get("version"),
            "output": (proc.stdout or proc.stderr or "").strip(),
            "exit_code": proc.returncode,
        }

