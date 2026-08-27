"""Supervision of the Penpot docker-compose stack.

Trilium's `server.py` supervises **one process** it `Popen`'d itself and can
poll with `proc.poll()`. Penpot has no such process here: the compose project
is five containers whose lifecycle belongs to `dockerd`, not to this Python
process, launched by handing `docker compose` an argv and reading its exit
code. That is the substantive difference from Trilium's shape, and it
propagates into three places:

- **Health is polled via `docker compose ps`, never a pid.** `reconcile()`
  classifies each expected service from the compose project's own container
  state (running/exited, plus a healthcheck's `healthy`/`unhealthy` when the
  image declares one) rather than asking whether a single subprocess is
  alive.
- **The containers are not parented to this process.** Trilium's child runs
  in its own session with `PR_SET_PDEATHSIG` specifically so it dies when the
  supervisor does — an awm restart must not leave an orphaned Trilium
  listening on the vault's port forever. Docker containers already have a
  well-defined owner (`dockerd`) independent of whatever awm process happens
  to be talking to them, so a hub_adapter restart (or even a full gateway
  restart) does not, and must not, take the stack down with it.
- **No respawn duplicate of compose's own `restart:` policy.** `reconcile()`
  is a report, not an action — restarting an individual crashed container is
  each container's own `restart:` policy business, set in the compose files
  this module treats as opaque input. The one thing this module does nudge
  is the *whole stack* coming back from a clean `down` (see
  `_health_loop` in `hub_adapter.py`), which is compose's own idempotent
  `up -d`, not a hand-rolled process supervisor.

**Configurable, not hardcoded.** The compose directory, project name, base
file and override files are all environment-overridable (`PENPOT_COMPOSE_*`)
so this module can be installed unmodified on a second host — sirius,
eventually — that checks the Penpot fork out to a different path or names its
overlay files differently. The defaults match the only place this runs today:
`projects/penpot/dev/docker/images/`, project `penpot-local`.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from awm.config import WORKSPACE_ROOT

log = logging.getLogger("awm.penpot.stack")

CommandResult = subprocess.CompletedProcess
#: Anything with this call shape can stand in for `subprocess.run` — tests
#: inject a fake so `tests/test_stack.py` never spawns a real `docker`.
Runner = Callable[..., CommandResult]


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


def _env_tuple(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return tuple(p.strip() for p in raw.split(",") if p.strip())


#: Where the compose files live. A different host points this at its own
#: checkout of the fork rather than editing this module.
COMPOSE_DIR = _env_path(
    "PENPOT_COMPOSE_DIR",
    WORKSPACE_ROOT / "projects" / "penpot" / "dev" / "docker" / "images",
)

#: Distinct from the fork's devenv project (which runs from a different
#: compose directory entirely) so `docker compose down` in one tree can never
#: reach the other's containers or network.
COMPOSE_PROJECT = os.environ.get("PENPOT_COMPOSE_PROJECT", "penpot-local")

COMPOSE_FILE = os.environ.get("PENPOT_COMPOSE_FILE", "docker-compose.yaml")

#: Applied in order after the base file. The local override carries the
#: locally-built image tags, the loopback-bound published ports and the
#: memory caps — see `projects/penpot/dev/docker/images/docker-compose.local.yml`.
#: Comma-separated so a future host can add or swap overlays with an env var,
#: not a code change.
OVERRIDE_FILES = _env_tuple(
    "PENPOT_COMPOSE_OVERRIDE_FILES", ("docker-compose.local.yml",)
)

ENV_FILE = os.environ.get("PENPOT_COMPOSE_ENV_FILE", ".env.local")

#: The slimmed-down set: no penpot-mcp, no penpot-mailcatch (both removed
#: deliberately — see the plan's T4.5). Comma-separated override so a future
#: change to the container set doesn't require editing this module.
EXPECTED_SERVICES = _env_tuple(
    "PENPOT_EXPECTED_SERVICES",
    ("penpot-frontend", "penpot-backend", "penpot-exporter",
     "penpot-postgres", "penpot-valkey"),
)

#: Where a held stop is recorded, so it survives this process. The gateway
#: respawns a service on any crash, deploy or restart, and an operator who
#: stopped the stack to free memory does not expect the next respawn to bring
#: it back.
HOLD_FILE = _env_path(
    "PENPOT_HOLD_FILE",
    Path(os.environ.get("AWM_DIR", "~/.awm")).expanduser() / "penpot" / "held",
)

HEALTH_INTERVAL_S = float(os.environ.get("PENPOT_HEALTH_INTERVAL_S", "20"))
#: `up -d`/`down` are usually fast once images are built, but a cold `up -d`
#: can still pull/create five containers — give it real headroom rather than
#: inheriting a generic client timeout.
START_TIMEOUT_S = float(os.environ.get("PENPOT_START_TIMEOUT_S", "180"))
COMMAND_TIMEOUT_S = float(os.environ.get("PENPOT_COMMAND_TIMEOUT_S", "60"))


def _default_runner(args: Sequence[str], **kwargs: Any) -> CommandResult:
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("timeout", COMMAND_TIMEOUT_S)
    return subprocess.run(list(args), **kwargs)


@dataclass(frozen=True)
class StackConfig:
    """Where the compose project lives and what it is called.

    A dataclass rather than bare module constants because a caller building a
    `Stack` for a second host (or a test) needs to pass a whole config, not
    monkeypatch five globals — see the module docstring's configurability
    requirement.
    """

    compose_dir: Path = COMPOSE_DIR
    project: str = COMPOSE_PROJECT
    compose_file: str = COMPOSE_FILE
    override_files: tuple[str, ...] = OVERRIDE_FILES
    env_file: str | None = ENV_FILE
    services: tuple[str, ...] = EXPECTED_SERVICES

    @property
    def exists(self) -> bool:
        """Whether the compose project's base file is on disk."""
        return (self.compose_dir / self.compose_file).is_file()

    def compose_args(self, *extra: str) -> list[str]:
        """The `docker compose ...` argv prefix, before a subcommand.

        Built in one place so `up -d`, `down`, `ps` and `logs` can never
        disagree on project name or override files — a mismatch there is how
        a `down` ends up targeting (or missing) the wrong stack.
        """
        args = ["docker", "compose", "-p", self.project, "-f", self.compose_file]
        for f in self.override_files:
            args += ["-f", f]
        if self.env_file:
            args += ["--env-file", self.env_file]
        args += list(extra)
        return args


#: The one stack this node supervises. There is no fleet: one compose
#: project, one set of containers.
CONFIG = StackConfig()


def _container_up(info: dict[str, Any]) -> bool:
    """Whether one container counts as up.

    A declared healthcheck is authoritative when present (`healthy` only);
    without one, `State == "running"` is the best this module can do — most
    of these images (postgres, valkey) ship no healthcheck of their own.
    """
    health = (info.get("health") or "").strip().lower()
    if health:
        return health == "healthy"
    return (info.get("state") or "").strip().lower() == "running"


def _parse_ps_output(stdout: str) -> list[dict[str, Any]]:
    """Compose's `ps --format json` output, tolerant of both shapes it ships.

    Some compose versions print one JSON object per line; others print a
    single JSON array. Guessing wrong here would silently read every
    container as absent — the same failure class as "stack looks stopped
    when it is actually fine" — so both shapes are tried before giving up on
    a line.
    """
    stdout = stdout.strip()
    if not stdout:
        return []
    try:
        parsed = json.loads(stdout)
        return parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        pass
    entries: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("penpot: unparsable `docker compose ps` line: %.200s", line)
    return entries


class Stack:
    """Supervises the Penpot compose project. Every method is safe to call at
    any time."""

    def __init__(self, config: StackConfig | None = None, *,
                 runner: Runner | None = None) -> None:
        self.config = config or CONFIG
        self._run: Runner = runner or _default_runner
        self._lock = threading.RLock()
        self._last_error: str | None = None
        # Set while something needs the stack to stay down — mirrors
        # Trilium's `Child._held`, released by the next `start`. Read back
        # from disk so a gateway respawn does not quietly un-hold a stack an
        # operator deliberately stopped.
        self._hold_file = HOLD_FILE
        self._held = self._hold_file.exists()

    # -- compose invocation ---------------------------------------------

    def _compose(self, *args: str, timeout: float | None = None) -> CommandResult:
        argv = self.config.compose_args(*args)
        log.debug("penpot: %s", " ".join(argv))
        return self._run(argv, cwd=str(self.config.compose_dir),
                          timeout=timeout or COMMAND_TIMEOUT_S)

    # -- state ------------------------------------------------------------

    def containers(self) -> dict[str, dict[str, Any]]:
        """Per-service container state, from `docker compose ps --all`.

        `--all` is load-bearing: without it, a container compose only
        *stopped* (rather than removed by `down`) simply would not appear,
        and this method could not tell "crashed, still present" from "never
        created" — which is exactly the distinction `reconcile()` needs to
        report unhealthy vs. stopped correctly.

        Returns `{}` on any failure to reach `docker compose` at all — the
        caller (`reconcile`) treats that the same as "nothing running" rather
        than raising, since a missing `docker` binary and a genuinely stopped
        stack must both surface as "not up", not as an exception a caller has
        to separately handle.
        """
        try:
            result = self._compose("ps", "--all", "--format", "json")
        except (OSError, subprocess.SubprocessError) as exc:
            self._last_error = f"docker compose ps failed: {exc}"
            return {}
        if result.returncode != 0:
            self._last_error = ((result.stderr or "").strip()
                                or f"docker compose ps exited {result.returncode}")
            return {}
        containers: dict[str, dict[str, Any]] = {}
        for entry in _parse_ps_output(result.stdout or ""):
            name = entry.get("Service") or entry.get("Name")
            if not name:
                continue
            containers[name] = {
                "state": entry.get("State"),
                "health": entry.get("Health") or None,
                "exit_code": entry.get("ExitCode"),
            }
        return containers

    def reconcile(self) -> dict[str, Any]:
        """Classify the stack from container state. Cheap; called on a loop.

        Three outcomes, not two, because "nothing is up" and "some of it
        crashed" call for different operator reactions (start vs.
        investigate):

        - `stopped` — no expected container is present at all (a clean
          `down`, or one that never ran `up -d`).
        - `unhealthy` — at least one expected container is missing or not up
          while at least one other is.
        - `healthy` — every expected service is present and up.
        """
        with self._lock:
            containers = self.containers()
            present = {name: containers[name] for name in self.config.services
                       if name in containers}
            missing = [name for name in self.config.services if name not in containers]
            unhealthy = [name for name, info in present.items()
                         if not _container_up(info)]
            if not present:
                stack_state = "stopped"
            elif missing or unhealthy:
                stack_state = "unhealthy"
            else:
                stack_state = "healthy"
            return {
                "stack_state": stack_state,
                "containers": containers,
                "missing": missing,
                "unhealthy": unhealthy,
                "error": self._last_error,
            }

    def status(self, *, verbose: bool = True) -> dict[str, Any]:
        """What the stack is doing.

        `verbose=False` drops the compose directory and per-container detail
        — a collaborator reading a page needs "up" or "not up", not a map of
        this host's filesystem. Mirrors Trilium's `snapshot(verbose=...)`.
        """
        state = self.reconcile()
        out: dict[str, Any] = {
            "running": state["stack_state"] != "stopped",
            "stack_state": state["stack_state"],
            "error": state["error"],
            # Whether a stop is being *held* — i.e. the supervision loop will
            # leave the stack down rather than bring it back. Without this an
            # operator cannot tell a stack that will stay stopped from one
            # that is seconds away from being restarted under them.
            "held": self._held,
        }
        if verbose:
            out.update({
                "project": self.config.project,
                "compose_dir": str(self.config.compose_dir),
                "containers": state["containers"],
                "missing": state["missing"],
                "unhealthy": state["unhealthy"],
            })
        return out

    # -- lifecycle ----------------------------------------------------------

    def start(self, *, wait: bool = True) -> dict[str, Any]:
        with self._lock:
            self._set_held(False)
            if not self.config.exists:
                self._last_error = (f"no compose file at "
                                    f"{self.config.compose_dir / self.config.compose_file}")
                raise FileNotFoundError(self._last_error)
            result = self._compose("up", "-d", timeout=START_TIMEOUT_S)
            if result.returncode != 0:
                self._last_error = ((result.stderr or "").strip()
                                    or f"docker compose up exited {result.returncode}")
                raise RuntimeError(self._last_error)
            self._last_error = None
        if wait:
            self._await_healthy()
        return self.status() | {"action": "started"}

    def _await_healthy(self) -> None:
        deadline = time.time() + START_TIMEOUT_S
        while time.time() < deadline:
            if self.reconcile()["stack_state"] == "healthy":
                return
            time.sleep(2.0)
        self._last_error = f"stack did not reach healthy within {START_TIMEOUT_S}s"

    def stop(self, *, hold: bool = False) -> dict[str, Any]:
        """`down`, not `stop` — this removes the containers rather than
        pausing them, which is what makes a deliberate stop distinguishable
        from a crash in `reconcile()`'s three-way classification (see there).

        A `hold` is recorded only once the stack is actually down, and on
        disk as well as in memory. Setting it first would leave a failed
        `down` reporting a held stop over running containers; keeping it only
        in memory would lose it the moment the gateway respawns this service,
        which is exactly when an operator is least likely to be watching.
        """
        with self._lock:
            result = self._compose("down", timeout=START_TIMEOUT_S)
            if result.returncode != 0:
                self._last_error = ((result.stderr or "").strip()
                                    or f"docker compose down exited {result.returncode}")
                return {"action": "stop-failed", "error": self._last_error}
            self._last_error = None
            self._set_held(hold)
        return self.status() | {"action": "stopped"}

    @property
    def held(self) -> bool:
        """Whether a deliberate stop is being held. Survives this process."""
        return self._held

    @property
    def hold_file(self) -> Path:
        """Where that hold is recorded, for an operator to see or clear."""
        return self._hold_file

    def _set_held(self, held: bool) -> None:
        """Record the hold in memory and on disk. Never raises: a hold that
        cannot be persisted is still worth honouring for this process's
        lifetime, and failing the stop over it would be worse."""
        self._held = held
        try:
            if held:
                self._hold_file.parent.mkdir(parents=True, exist_ok=True)
                self._hold_file.write_text("held\n")
            else:
                self._hold_file.unlink(missing_ok=True)
        except OSError:
            log.warning("penpot: could not persist hold state to %s",
                        self._hold_file, exc_info=True)

    def restart(self) -> dict[str, Any]:
        self.stop()
        return self.start() | {"action": "restarted"}

    def logs(self, *, service: str | None = None, tail: int = 200) -> str:
        """Tail the containers' own logs via `docker compose logs`.

        Unlike Trilium's `Child.logs`, there is no file this module writes
        and reads back — Docker already retains each container's stdout/
        stderr, so re-capturing it into a service-owned log file would just
        be a second, driftable copy of what `docker logs` already holds.
        """
        args = ["logs", "--no-color", "--tail", str(int(tail))]
        if service:
            args.append(service)
        try:
            result = self._compose(*args, timeout=COMMAND_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"(docker compose logs failed: {exc})"
        return (result.stdout or "") + (result.stderr or "")


#: The one stack this node supervises.
STACK = Stack()
