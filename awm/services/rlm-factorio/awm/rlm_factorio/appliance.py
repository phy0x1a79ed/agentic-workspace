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

# --- single-session fixed coordinates (the pool seam) ---------------------

PROJECT = "rlm-factorio"            # docker compose -p <project>
CONTAINER = "rlm-factorio-appliance"
GAME_PORT = 12140                   # Factorio UDP (Steam-join)
CONTROL_PORT = 12142                # supervisor HTTP control surface
RCON_PORT = 0                       # 0 until the RCON pass lands
SAVES_VOL = "rlm-factorio-saves"    # named volume = the sacred-saves store

# appliance/ sits at the service root, beside the awm/ package dir:
#   awm/services/rlm-factorio/appliance/docker-compose.yml
#   awm/services/rlm-factorio/awm/rlm_factorio/appliance.py  (this file)
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = _SERVICE_ROOT / "appliance" / "docker-compose.yml"

# Bring-up budget: compose up --build can pull/build a large image on first run;
# the engine then needs to load (and possibly generate) a world before (InGame).
READY_TIMEOUT = float(900)          # seconds for the supervisor to report ready
CONTROL_TIMEOUT = 300.0             # per-request timeout for world ops


class ApplianceError(RuntimeError):
    """A docker compose or supervisor control call failed."""


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


def compose_up(project: str, env: dict[str, str]) -> None:
    """Build (first run) + start the appliance container, detached."""
    # No explicit timeout: a first-run image build legitimately takes minutes.
    _compose(project, env, "up", "-d", "--build")


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
