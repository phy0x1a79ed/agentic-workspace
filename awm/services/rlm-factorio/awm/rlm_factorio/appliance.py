"""Appliance control: bring the Factorio container up/down and reach its
supervisor's HTTP control surface.

The realm owns the substrate. ``acquire`` brings up a ``docker compose`` project
(building the image on first run); the per-session container then runs the
stdlib supervisor (``appliance/supervise.py``) which owns the engine and exposes
``GET /status`` + ``POST /save|/new|/load`` on a control port. These helpers are
the only place that shells out to Docker or talks to that control port, so the
handlers in :mod:`hub_adapter` stay thin.

Each world is its own ``docker compose -p <project>``. The base compose file is
shared; a generated per-world override makes the world its kind (published game
port, lab mods and saves, boot options).

Handlers call these from a worker thread (ServiceAdapter runs sync handlers via
``asyncio.to_thread``), so the blocking ``subprocess`` + ``httpx.Client`` calls
here are safe.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import httpx

from awm.persistence.databases import SERVICES_DIR

log = logging.getLogger("awm.rlm_factorio.appliance")

# --- the realm's coordinates ---------------------------------------------
#
# The world named ``realm`` keeps the coordinates it always had, so its compose
# project still owns `rlm-factorio_factorio-saves`, the volume the user's actual
# worlds live in. Every other world gets a project, container and control port
# of its own. A test rig MUST override these: a harness left on the default
# project runs its throwaway worlds against the sacred saves.

PROJECT = os.environ.get("AWM_FACTORIO_PROJECT", "rlm-factorio")
CONTAINER = os.environ.get("AWM_FACTORIO_CONTAINER", "rlm-factorio-appliance")
GAME_PORT = int(os.environ.get("AWM_FACTORIO_GAME_PORT", "12140"))
CONTROL_PORT = int(os.environ.get("AWM_FACTORIO_CONTROL_PORT", "12142"))
RCON_PORT = 0                       # container-internal only, never published
                                    # (the supervisor owns RCON; see supervise.py)
SAVES_VOL = "rlm-factorio-saves"    # named volume = the sacred-saves store
REALM_NAME = "realm"
REALM_VOLUME = f"{PROJECT}_factorio-saves"

# Control and game ports for every world but the realm come from here.
PORT_BASE = int(os.environ.get("AWM_FACTORIO_PORT_BASE", "12150"))
PORT_SLOTS = 50

KINDS = ("headless", "lab", "rendered")
WORLD_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")

# appliance/ sits at the service root, beside the awm/ package dir:
#   awm/services/rlm-factorio/appliance/docker-compose.yml
#   awm/services/rlm-factorio/awm/rlm_factorio/appliance.py  (this file)
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
APPLIANCE_DIR = _SERVICE_ROOT / "appliance"
COMPOSE_FILE = APPLIANCE_DIR / "docker-compose.yml"
BUILD_SCRIPT = APPLIANCE_DIR / "build.sh"
IMAGE = "rlm-factorio/appliance:2.1.20"   # must match docker-compose.yml
# The control mod is bind-mounted into the host and the camera from this one
# directory, so a mod change ships with a container restart instead of an image
# rebuild. Host and camera must load identical bytes or the camera cannot join.
MOD_DIR = Path(os.environ.get(
    "AWM_FACTORIO_MOD_DIR",
    str(APPLIANCE_DIR / "mods" / "game-bot-control")))
MOD_MOUNT = "/opt/factorio/mods/game-bot-control"
MODS_ROOT = "/opt/factorio/mods"

# A lab loads the fxlab mods beside the control mod. They live with the lab
# tooling in game-bot, which owns them.
LAB_MODS_DIR = Path(os.path.expanduser(os.environ.get(
    "RLM_FACTORIO_LAB_MODS",
    "~/agentic_workspace/projects/game-bot/factorio/research/lab/fxlab/mods")))
LAB_MODS = ("fxlab-sbx", "fxlab-census")
CAMERA_CONFIG = APPLIANCE_DIR / "config" / "config-camera.ini"

# Bring-up budget: the engine needs to load (and possibly generate) a world
# before it reports (InGame). The first-run image build is budgeted separately
# (BUILD_TIMEOUT) because it downloads ~4.4 GB of game assets.
READY_TIMEOUT = float(900)          # seconds for the supervisor to report ready
BUILD_TIMEOUT = 3600.0              # seconds for a cold image build
CONTROL_TIMEOUT = 300.0             # per-request timeout for world ops


class ApplianceError(RuntimeError):
    """A docker compose or supervisor control call failed."""


def output_root() -> Path:
    """Root of everything the engine and its camera write out for us."""
    return SERVICES_DIR / "rlm-factorio" / "output"


def session_output_dir(session_id: str) -> Path:
    """Host directory bind-mounted as the HOST engine's ``script-output``."""
    path = output_root() / session_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def world_dir(session_id: str) -> Path:
    """Host directory holding a world's compose override and, for a lab, its saves."""
    path = SERVICES_DIR / "rlm-factorio" / "worlds" / session_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def saves_dir(row: dict) -> Path | None:
    """The host directory a lab's saves are bound to; None for a volume-backed world."""
    if row.get("kind") != "lab":
        return None
    path = world_dir(row["session_id"]) / "saves"
    path.mkdir(parents=True, exist_ok=True)
    return path


def world_coords(kind: str, name: str, taken_ports: set[int]) -> dict:
    """Project, container and ports for a new world.

    The realm keeps its fixed coordinates. Every other world takes the first
    free control port in its range, and the game port beside it.
    """
    if name == REALM_NAME and kind != "lab":
        return {"compose_project": PROJECT, "container_name": CONTAINER,
                "control_port": CONTROL_PORT, "game_port": GAME_PORT}
    project = f"{PROJECT}-{'lab' if kind == 'lab' else 'world'}-{name}"
    for slot in range(PORT_SLOTS):
        control = PORT_BASE + 2 * slot
        if control not in taken_ports and control + 1 not in taken_ports:
            return {"compose_project": project, "container_name": f"{project}-appliance",
                    "control_port": control, "game_port": control + 1}
    raise ApplianceError(f"no free port pair left in {PORT_BASE}-{PORT_BASE + 2 * PORT_SLOTS}")


def world_mods(kind: str) -> list[str]:
    """Extra mod directories a world of this kind loads beside the control mod."""
    if kind != "lab":
        return []
    return [str(LAB_MODS_DIR / name) for name in LAB_MODS]


def row_mods(row: dict) -> list[str]:
    raw = row.get("mods") or "[]"
    return list(json.loads(raw) if isinstance(raw, str) else raw)


def compose_env(row: dict) -> dict[str, str]:
    """``FACTORIO_*`` env that parameterizes the compose file for this world."""
    lab = row.get("kind") == "lab"
    return {
        "FACTORIO_CONTAINER": row.get("container_name") or CONTAINER,
        "FACTORIO_GAME_PORT": str(row.get("game_port") or GAME_PORT),
        "FACTORIO_CONTROL_PORT": str(row.get("control_port") or CONTROL_PORT),
        "FACTORIO_OUTPUT_DIR": str(session_output_dir(
            row.get("session_id") or "default")),
        "FACTORIO_MOD_DIR": str(MOD_DIR),
        "FACTORIO_CPUS": "2.0" if lab else "4.0",
        "FACTORIO_MEMORY": "4g" if lab else "6g",
    }


def write_override(row: dict, boot: dict | None = None) -> Path:
    """Write the compose override that makes this world its kind; return its path.

    Only a rendered world publishes the game port. A lab binds its saves to a
    host directory, loads the lab mods, and runs the lab's server and map
    settings. ``boot`` picks the world the supervisor starts on. The file is
    JSON, which compose reads as YAML.
    """
    boot = boot or {}
    service: dict = {"environment": {
        "BOOT_SAVE": boot.get("save") or "",
        "BOOT_NEW": "1" if boot.get("create") else "",
        "BOOT_PAUSED": "1" if boot.get("paused") else "",
    }}
    volumes = []
    if row.get("kind") == "rendered":
        service["ports"] = [f"{row.get('game_port') or GAME_PORT}:12140/udp"]
    if row.get("kind") == "lab":
        volumes.append(f"{saves_dir(row)}:/factorio/saves")
        for cfg in ("server-settings", "map-gen-settings"):
            volumes.append(f"{APPLIANCE_DIR / 'config' / f'lab-{cfg}.json'}"
                           f":/factorio/config/{cfg}.json:ro")
    for mod in row_mods(row):
        volumes.append(f"{mod}:{MODS_ROOT}/{Path(mod).name}:ro")
    if volumes:
        service["volumes"] = volumes
    path = world_dir(row["session_id"]) / "compose.json"
    path.write_text(json.dumps({"services": {"factorio": service}}, indent=2))
    return path


def compose_files(row: dict) -> list[Path]:
    override = world_dir(row["session_id"]) / "compose.json"
    return [COMPOSE_FILE, override] if override.exists() else [COMPOSE_FILE]


def _compose(row: dict, *args: str,
             timeout: float | None = None) -> subprocess.CompletedProcess:
    files = [x for f in compose_files(row) for x in ("-f", str(f))]
    cmd = ["docker", "compose", "-p", row.get("compose_project") or PROJECT, *files, *args]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, **compose_env(row)},
    )
    if proc.returncode != 0:
        raise ApplianceError(
            f"`{' '.join(cmd)}` failed (exit {proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


def stage_save(row: dict, save: str) -> str:
    """Put ``save`` where a lab's supervisor boots from; return the save's name.

    ``save`` is the name of a save in the realm's volume, or a path to a .zip
    on this machine. The realm volume is mounted read-only, so a lab can never
    write a real world.
    """
    dest = saves_dir(row)
    if dest is None:
        return save
    src = Path(os.path.expanduser(save))
    if save.endswith(".zip") and src.is_file():
        shutil.copy(src, dest / src.name)
        return src.stem
    proc = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{REALM_VOLUME}:/s:ro", "-v", f"{dest}:/o",
         "alpine:3", "cp", f"/s/{save}.zip", "/o/"],
        capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise ApplianceError(f"no realm save named {save!r}: {proc.stderr.strip()}")
    return save


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


def compose_up(row: dict, boot: dict | None = None) -> None:
    """Start the world's container, detached, building the image if needed."""
    ensure_image()
    write_override(row, boot)
    _compose(row, "up", "-d", "--force-recreate")


def compose_down(row: dict) -> None:
    """Stop + remove the appliance container. The named saves volume is kept
    (no ``-v``) — it is the sacred-saves store and must survive release."""
    _compose(row, "down", timeout=120)


def is_container_running(row: dict) -> bool:
    """True if the world's compose project has a running container."""
    try:
        proc = _compose(row, "ps", "-q", "--status", "running", timeout=30)
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


# --- the camera ---------------------------------------------------------
#
# Agents act through drones the world itself holds, so the only client a world
# runs is its camera: a container running the SAME image in its client role
# (FACTORIO_ROLE=seat) as a spectator with no character. The server cannot
# render, and take_screenshot is drawn by the client it names. The camera is
# run directly, not declared in the compose file, and joins over the world's
# own compose network by the host's service DNS name, so its traffic never
# leaves Docker.

SEAT_NETWORK_SUFFIX = "_default"    # docker compose's implicit network
SEAT_HOST_ALIAS = "factorio"        # the compose service name of the host
SEAT_GAME_PORT = 12140              # container-internal, fixed

# Where the camera's screenshots land. Factorio writes them inside the peer
# that rendered them, so the camera bind-mounts its own script-output onto the
# host and the service reads the file directly.
SEAT_OUTPUT_DIR = "/opt/factorio/script-output"

CAMERA_CPUS, CAMERA_MEMORY = "2", "4g"
CACHE_FILES = ("atlas-cache.dat", "crop-cache.dat")
CACHE_BUILD_TIMEOUT = float(os.environ.get("AWM_FACTORIO_CACHE_BUILD_TIMEOUT", "900"))
CLIENT_LOG = "/opt/factorio/factorio-current.log"
_CACHE_LOCK = threading.Lock()


def seat_output_root() -> Path:
    return output_root() / "seats"


def seat_output_dir(seat_id: str) -> Path:
    """Host directory holding one camera's rendered output (created on demand)."""
    path = seat_output_root() / seat_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def seat_network(project: str) -> str:
    return f"{project or PROJECT}{SEAT_NETWORK_SUFFIX}"


def seat_container_name(project: str, seat_id: str) -> str:
    return f"{project or PROJECT}-{seat_id}"


def camera_cache_root() -> Path:
    return output_root() / "camera-cache"


def _mod_mounts(mods: list[str]) -> list[str]:
    mounts = ["-v", f"{MOD_DIR}:{MOD_MOUNT}:ro"]
    for mod in mods:
        mounts += ["-v", f"{mod}:{MODS_ROOT}/{Path(mod).name}:ro"]
    return mounts


def camera_cache_key(mods: list[str]) -> str:
    """The image tag and every loaded mod's version: a sprite cache built for
    any other set makes the client rewrite it, which a read-only mount stalls."""
    versions = []
    for mod in [str(MOD_DIR), *mods]:
        try:
            info = json.loads((Path(mod) / "info.json").read_text())
            versions.append(f"{info['name']}@{info['version']}")
        except (OSError, ValueError, KeyError):
            versions.append(f"{Path(mod).name}@?")
    digest = hashlib.sha1("\n".join(sorted(versions)).encode()).hexdigest()[:10]
    return f"{IMAGE.rsplit(':', 1)[-1]}-{digest}"


def _docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=timeout)


def build_camera_cache(dest: Path, mods: list[str]) -> None:
    """Run a client with no network until it saves its sprite caches, then copy
    them to ``dest``. The client loads every sprite before it tries to connect,
    so it needs no world."""
    container = f"{PROJECT}-camera-cache-{uuid.uuid4().hex[:8]}"
    proc = _docker(
        "run", "-d", "--name", container, "--network", "none",
        "-e", "FACTORIO_ROLE=seat", "-e", "SEAT_NAME=camera-cache",
        "-e", "FACTORIO_HOST=127.0.0.1", "-e", "SEAT_UNMAP=0",
        "--cpus", "4", "--memory", "6g", *_mod_mounts(mods),
        "-v", f"{CAMERA_CONFIG}:/opt/factorio/config/config.ini:ro", IMAGE)
    if proc.returncode != 0:
        raise ApplianceError(f"camera cache build failed to start: {proc.stderr.strip()}")
    try:
        deadline = time.monotonic() + CACHE_BUILD_TIMEOUT
        while True:
            log = _docker("exec", container, "cat", CLIENT_LOG, timeout=30).stdout
            if "Atlas cache saved" in log:
                break
            if time.monotonic() > deadline or not seat_container_running(container):
                raise ApplianceError(
                    f"camera cache build saved no atlas cache: {log[-800:]}")
            time.sleep(2.0)
        staging = dest.with_name(dest.name + ".partial")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        for name in CACHE_FILES:
            cp = _docker("cp", f"{container}:/opt/factorio/{name}", str(staging / name))
            if cp.returncode != 0:
                raise ApplianceError(f"camera cache build wrote no {name}: {cp.stderr.strip()}")
        staging.rename(dest)
    finally:
        _docker("rm", "-f", "-v", container)


def camera_cache(mods: list[str]) -> Path | None:
    """The sprite cache for this mod set, built on first use. None when the
    build fails: a camera without a cache builds its own, only slower."""
    dest = camera_cache_root() / camera_cache_key(mods)
    with _CACHE_LOCK:
        if not all((dest / name).exists() for name in CACHE_FILES):
            try:
                build_camera_cache(dest, mods)
            except (ApplianceError, OSError, subprocess.SubprocessError) as exc:
                log.warning("camera cache %s: %s", dest.name, exc)
                return None
    return dest


def _camera_mounts(mods: list[str]) -> list[str]:
    """Sprite caches mount read-only, so two cameras never race on a write.
    Mesa's shader cache is multi-process safe."""
    shaders = camera_cache_root() / "mesa_shader_cache"
    shaders.mkdir(parents=True, exist_ok=True)
    mounts = ["-v", f"{CAMERA_CONFIG}:/opt/factorio/config/config.ini:ro",
              "-v", f"{shaders}:/root/.cache/mesa_shader_cache"]
    cache = camera_cache(mods)
    for name in CACHE_FILES if cache else ():
        mounts += ["-v", f"{cache / name}:/opt/factorio/{name}:ro"]
    return mounts


def camera_run(container: str, network: str, player_name: str,
               *, output_dir: Path, mods: list[str] = ()) -> str:
    """Start the camera joined to ``network``; return its container id. It
    loads the host's extra ``mods`` too, or it cannot join."""
    proc = _docker(
        "run", "-d", "--name", container, "--network", network,
        "-e", "FACTORIO_ROLE=seat", "-e", f"SEAT_NAME={player_name}",
        "-e", f"FACTORIO_HOST={SEAT_HOST_ALIAS}", "-e", f"FACTORIO_PORT={SEAT_GAME_PORT}",
        "-e", "SEAT_UNMAP=1", "--cpus", CAMERA_CPUS, "--memory", CAMERA_MEMORY,
        *_mod_mounts(list(mods)), *_camera_mounts(list(mods)),
        "-v", f"{output_dir}:{SEAT_OUTPUT_DIR}", IMAGE)
    if proc.returncode != 0:
        raise ApplianceError(
            f"camera container {container} failed to start: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout.strip()


def seat_stop(container: str) -> None:
    """Remove a camera container. Best-effort: one that is already gone is the
    outcome we wanted."""
    _docker("rm", "-f", "-v", container)


def seat_container_running(container: str) -> bool:
    if not container:
        return False
    proc = _docker("inspect", "-f", "{{.State.Running}}", container, timeout=30)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def seat_logs(container: str, tail: int = 40) -> str:
    proc = _docker("logs", "--tail", str(tail), container, timeout=30)
    return (proc.stdout or "") + (proc.stderr or "")
