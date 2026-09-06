"""Appliance control: bring the Factorio container up/down and reach its
supervisor's HTTP control surface.

The realm owns the substrate. ``acquire`` brings up a ``docker compose`` project
(building the image on first run); the per-session container then runs the
stdlib supervisor (``appliance/supervise.py``) which owns the engine and exposes
``GET /status`` + ``POST /save|/new|/load`` on a control port. These helpers are
the only place that shells out to Docker or talks to that control port, so the
handlers in :mod:`hub_adapter` stay thin and the eventual session-pool change
(per-session ``-p <project>`` + allocated ports) is localized here.

Single-session defaults for now: one fixed compose project / container / ports /
saves-volume. The compose file is parameterized by ``FACTORIO_*`` env vars so a
pool pass only needs to vary those per session and drop the "at most one" guard
in the adapter — no contract or schema change.

Handlers call these from a worker thread (ServiceAdapter runs sync handlers via
``asyncio.to_thread``), so the blocking ``subprocess`` + ``httpx.Client`` calls
here are safe.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import httpx

from awm.persistence.databases import SERVICES_DIR

# --- single-session fixed coordinates (the pool seam) ---------------------
#
# Overridable from the environment, and a test rig MUST override them. The
# default compose project owns `rlm-factorio_factorio-saves`, the volume the
# user's actual worlds live in, so a harness left on the default project runs
# its throwaway worlds against the sacred saves and its teardown is one `down
# -v` away from destroying them.

PROJECT = os.environ.get("AWM_FACTORIO_PROJECT", "rlm-factorio")
CONTAINER = os.environ.get("AWM_FACTORIO_CONTAINER", "rlm-factorio-appliance")
GAME_PORT = int(os.environ.get("AWM_FACTORIO_GAME_PORT", "12140"))
CONTROL_PORT = int(os.environ.get("AWM_FACTORIO_CONTROL_PORT", "12142"))
RCON_PORT = 0                       # container-internal only, never published
                                    # (the supervisor owns RCON; see supervise.py)
SAVES_VOL = "rlm-factorio-saves"    # named volume = the sacred-saves store

# appliance/ sits at the service root, beside the awm/ package dir:
#   awm/services/rlm-factorio/appliance/docker-compose.yml
#   awm/services/rlm-factorio/awm/rlm_factorio/appliance.py  (this file)
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = _SERVICE_ROOT / "appliance" / "docker-compose.yml"
BUILD_SCRIPT = _SERVICE_ROOT / "appliance" / "build.sh"
IMAGE = "rlm-factorio/appliance:2.1.17"   # must match docker-compose.yml

# Bring-up budget: the engine needs to load (and possibly generate) a world
# before it reports (InGame). The first-run image build is budgeted separately
# (BUILD_TIMEOUT) because it downloads ~4.4 GB of game assets.
READY_TIMEOUT = float(900)          # seconds for the supervisor to report ready
BUILD_TIMEOUT = 3600.0              # seconds for a cold image build
CONTROL_TIMEOUT = 300.0             # per-request timeout for world ops


class ApplianceError(RuntimeError):
    """A docker compose or supervisor control call failed."""


def output_root() -> Path:
    """Root of everything the engine and its seats write out for us."""
    return SERVICES_DIR / "rlm-factorio" / "output"


def session_output_dir(session_id: str) -> Path:
    """Host directory bind-mounted as the HOST engine's ``script-output``."""
    path = output_root() / session_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def compose_env(row: dict) -> dict[str, str]:
    """``FACTORIO_*`` env that parameterizes the compose file for this session.

    Only the container name + published host ports vary per session; the volume
    (and default container naming) is namespaced by ``docker compose -p`` for
    free, so it needs no env var here.
    """
    return {
        "FACTORIO_CONTAINER": row.get("container_name") or CONTAINER,
        "FACTORIO_GAME_PORT": str(row.get("game_port") or GAME_PORT),
        "FACTORIO_CONTROL_PORT": str(row.get("control_port") or CONTROL_PORT),
        "FACTORIO_OUTPUT_DIR": str(session_output_dir(
            row.get("session_id") or "default")),
    }


def _compose(project: str, env: dict[str, str], *args: str,
             timeout: float | None = None) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose", "-p", project, "-f", str(COMPOSE_FILE), *args]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, **env},
    )
    if proc.returncode != 0:
        raise ApplianceError(
            f"`{' '.join(cmd)}` failed (exit {proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


def image_exists() -> bool:
    proc = subprocess.run(["docker", "image", "inspect", IMAGE],
                          capture_output=True, text=True, timeout=60)
    return proc.returncode == 0


def ensure_image() -> None:
    """Build the appliance image if it is not present.

    Building is deliberately not folded into ``compose up --build``: the full
    Factorio build is behind account auth, so a build needs credentials that a
    running service may not have, and an unconditional ``--build`` would turn
    every acquire into a credential check. ``appliance/build.sh`` owns the
    authenticated build and explains itself when the credentials are missing.
    """
    if image_exists():
        return
    proc = subprocess.run([str(BUILD_SCRIPT)], capture_output=True, text=True,
                          timeout=BUILD_TIMEOUT)
    if proc.returncode != 0:
        raise ApplianceError(
            f"appliance image {IMAGE} is missing and `{BUILD_SCRIPT}` failed "
            f"(exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )


def compose_up(project: str, env: dict[str, str]) -> None:
    """Start the appliance container, detached, building the image if needed."""
    ensure_image()
    _compose(project, env, "up", "-d")


def compose_down(project: str, env: dict[str, str]) -> None:
    """Stop + remove the appliance container. The named saves volume is kept
    (no ``-v``) — it is the sacred-saves store and must survive release."""
    _compose(project, env, "down", timeout=120)


def is_container_running(project: str, env: dict[str, str]) -> bool:
    """True if the compose project has a running container."""
    try:
        proc = _compose(project, env, "ps", "-q", "--status", "running",
                        timeout=30)
    except ApplianceError:
        return False
    return bool(proc.stdout.strip())


def control_url(row: dict) -> str:
    port = int(row.get("control_port") or CONTROL_PORT)
    return f"http://127.0.0.1:{port}"


def status(row: dict, *, timeout: float = 10.0) -> dict | None:
    """Best-effort ``GET /status`` against the supervisor; None if unreachable."""
    try:
        resp = httpx.get(f"{control_url(row)}/status", timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def wait_ready(row: dict, timeout: float = READY_TIMEOUT) -> dict:
    """Poll the supervisor until it reports a running, ready engine.

    Tolerates connection-refused while the container's python is still coming
    up. Returns the final ``/status`` payload; raises on timeout.
    """
    deadline = time.monotonic() + timeout
    last: dict | None = None
    while time.monotonic() < deadline:
        last = status(row, timeout=5.0)
        if last and last.get("running") and last.get("ready"):
            return last
        time.sleep(2.0)
    raise ApplianceError(
        f"appliance did not become ready within {timeout:.0f}s "
        f"(last status: {last!r})"
    )


def control_post(row: dict, path: str, body: dict,
                 *, timeout: float = CONTROL_TIMEOUT) -> dict:
    """POST to a supervisor control route, surfacing its error envelope.

    The supervisor replies ``{ok: true, result: ...}`` on success or a non-2xx
    with ``{ok: false, error: ...}``; we translate the latter into an exception
    carrying its message so the adapter reports it back to the caller.
    """
    resp = httpx.post(f"{control_url(row)}{path}", json=body, timeout=timeout)
    try:
        data = resp.json()
    except Exception:
        data = {}
    if resp.status_code >= 400 or not data.get("ok", False):
        raise ApplianceError(
            data.get("error") or f"control {path} failed (HTTP {resp.status_code})"
        )
    return data.get("result", {})


def iface(row: dict, fn: str, args: dict,
          *, timeout: float = CONTROL_TIMEOUT) -> dict:
    """Call one ``game-bot-control`` interface function through the supervisor.

    A single route carries every world verb, so adding a mod capability costs a
    handler here and nothing in between.
    """
    return control_post(row, "/iface", {"fn": fn, "args": args}, timeout=timeout)


# --- seats ----------------------------------------------------------------
#
# A seat is a transient container running the SAME image in its client role
# (FACTORIO_ROLE=seat). Seats are not declared in the compose file: they are
# allocated per agent, come and go independently of the world, and are run
# directly. They join over the session's own compose network by the host's
# service DNS name, so seat traffic never leaves Docker and none of the
# published-port / WSL-forwarding pain applies -- only a human joining from
# Steam needs the published UDP port.

SEAT_NETWORK_SUFFIX = "_default"    # docker compose's implicit network
SEAT_HOST_ALIAS = "factorio"        # the compose service name of the host
SEAT_GAME_PORT = 12140              # container-internal, fixed

# Where a seat's screenshots land. Factorio writes them inside the peer that
# rendered them -- a client, not the server -- so each seat bind-mounts its own
# script-output onto the host and the service reads the file directly. Handing
# an agent a path beats handing it a base64 blob it has to decode by hand.
SEAT_OUTPUT_DIR = "/opt/factorio/script-output"

# Factorio has no frame-rate setting and Xvfb reports 0 Hz, so nothing paces a
# client's renderer: with its window mapped it renders flat out across several
# threads. Unmapping the window is the real lever, but it only takes effect
# once the client reaches the world, so a seat is briefly unbounded on the way
# in -- and a seat whose unmap fails would be unbounded forever. These bound
# that runaway; they are not a diet.
#
# Size them to the JOIN, not to play. Building the sprite atlas on the way in
# is the expensive part -- 15 threads and a ~3.7 GiB peak, measured -- while an
# in-world seat with its window unmapped settles to ~13% of one core and
# 2.4 GiB. At 2 cores / 4 GiB the atlas build stretched a 40s join past 300s
# and pressed against the memory cap. Squeezing here does not save a running
# seat anything; it only makes joining slow or impossible.
SEAT_CPUS = os.environ.get("AWM_FACTORIO_SEAT_CPUS", "4.0")
SEAT_MEMORY = os.environ.get("AWM_FACTORIO_SEAT_MEMORY", "6g")


def seat_output_root() -> Path:
    return output_root() / "seats"


def seat_output_dir(seat_id: str) -> Path:
    """Host directory holding one seat's rendered output (created on demand)."""
    path = seat_output_root() / seat_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def seat_network(project: str) -> str:
    return f"{project or PROJECT}{SEAT_NETWORK_SUFFIX}"


def seat_container_name(project: str, seat_id: str) -> str:
    return f"{project or PROJECT}-{seat_id}"


def seat_run(container: str, network: str, player_name: str,
             *, output_dir: Path | None = None,
             env: dict[str, str] | None = None) -> str:
    """Start a seat container joined to ``network``; return its container id."""
    cmd = [
        "docker", "run", "-d",
        "--name", container,
        "--network", network,
        "-e", "FACTORIO_ROLE=seat",
        "-e", f"SEAT_NAME={player_name}",
        "-e", f"FACTORIO_HOST={SEAT_HOST_ALIAS}",
        "-e", f"FACTORIO_PORT={SEAT_GAME_PORT}",
        "--cpus", SEAT_CPUS,
        "--memory", SEAT_MEMORY,
    ]
    if output_dir is not None:
        cmd += ["-v", f"{output_dir}:{SEAT_OUTPUT_DIR}"]
    for key, value in (env or {}).items():
        cmd += ["-e", f"{key}={value}"]
    cmd.append(IMAGE)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise ApplianceError(
            f"seat container {container} failed to start: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout.strip()


def seat_stop(container: str) -> None:
    """Remove a seat container. Best-effort: a seat that is already gone is the
    outcome we wanted."""
    subprocess.run(["docker", "rm", "-f", "-v", container],
                   capture_output=True, text=True, timeout=120)


def seat_container_running(container: str) -> bool:
    proc = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container],
        capture_output=True, text=True, timeout=30)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def seat_logs(container: str, tail: int = 40) -> str:
    proc = subprocess.run(["docker", "logs", "--tail", str(tail), container],
                          capture_output=True, text=True, timeout=30)
    return (proc.stdout or "") + (proc.stderr or "")
