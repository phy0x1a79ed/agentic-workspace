"""Filesystem discovery of feature services.

A service is *just a folder the gateway can run*: any subdirectory of the
services root that ships an executable, self-contained ``run.sh``. The gateway
treats the folder as a black box — it never imports it, never inspects its
language, and only ever launches it with ``bash run.sh`` (injecting
``AWM_HUB_URL`` / ``AWM_SERVICE_NAME`` / ``AWM_SERVICE_ID``). So a service can be
a Python process, a Rust binary, or a thin proxy to a remote API — discovery
only cares that ``run.sh`` exists.

The ``start_cmd`` / ``cwd`` a spec carries mirror exactly what a service
self-registers with through ``ServiceAdapter`` (``["bash", "run.sh"]`` +
``os.getcwd()``), so a bootstrap-spawned journal entry is indistinguishable from
a self-registered one — the supervisor reconcile/respawn path needs no special
case for either origin.

Enable/disable state lives in ``<AWM_DIR>/services/enabled.json`` (``{name:
bool}``, absent ⇒ enabled). It is kept apart from the ephemeral PID journal so a
disabled service stays down across a gateway restart.

Profiles (the gate that keeps effort-specific services off dev/prod): a service
folder may ship a committed ``service.toml`` with ``profiles = ["gamebot"]`` —
the list of gateway profiles that want it. The running gateway's active
profiles come from the ``AWM_PROFILES`` env (comma list; a composition sandbox
sets it in its gitignored dev ``.env``, prod sets none). Resolution precedence:

1. an explicit ``enabled.json`` entry always wins (both true and false — the
   operator's word, e.g. prod's deliberately-live rlm-browser);
2. else a marked service is enabled iff its profiles intersect the active set;
3. else (no marker — every pre-existing service) enabled.

A corrupt marker reads as marked-but-unmatched (disabled + logged), never
fail-open.
"""

from __future__ import annotations

import json
import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from awm import config

log = logging.getLogger("awm.hub.discovery")

# The fixed contract: a service folder is started by executing its run.sh.
RUN_SCRIPT = "run.sh"
START_CMD = ["bash", RUN_SCRIPT]


@dataclass(frozen=True)
class ServiceSpec:
    """A discovered service folder.

    ``start_cmd`` + ``cwd`` are exactly what the supervisor passes to
    ``spawn_service`` and exactly what the adapter self-registers, so the two
    spawn origins (bootstrap vs. self-register) are interchangeable.
    """

    name: str
    cwd: str
    enabled: bool
    start_cmd: list[str] = field(default_factory=lambda: list(START_CMD))


# ---------------------------------------------------------------------------
# Services root resolution
# ---------------------------------------------------------------------------

def services_root() -> Path:
    """Resolve the services tree.

    Anchored to the gateway's own on-disk location (not cwd / workspace), so the
    running gateway always manages the services that live in *its* worktree.
    ``AWM_SERVICES_DIR`` overrides it (tests point this at a temp tree).
    """
    if env := os.environ.get("AWM_SERVICES_DIR"):
        return Path(env).resolve()
    import awm.gateway
    here = Path(awm.gateway.__file__).resolve()
    for parent in here.parents:
        cand = parent / "services"
        if parent.name == "awm" and cand.is_dir():
            return cand
    # Fall back to the fixed nesting: <root>/awm/gateway/awm/gateway/__init__.py
    # → parents[3] == <root>/awm.
    return here.parents[3] / "services"


# ---------------------------------------------------------------------------
# Enable/disable state
# ---------------------------------------------------------------------------

def _enabled_path() -> Path:
    config.SERVICES_DIR.mkdir(parents=True, exist_ok=True)
    return config.SERVICES_DIR / "enabled.json"


def load_enabled() -> dict[str, bool]:
    """Return the ``{name: bool}`` enable map. Empty (⇒ all enabled) on a
    missing or corrupt file."""
    path = _enabled_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not parse %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: bool(v) for k, v in data.items()}


# ---------------------------------------------------------------------------
# Profile gate (committed service.toml marker × AWM_PROFILES env)
# ---------------------------------------------------------------------------

PROFILE_MARKER = "service.toml"


def active_profiles() -> set[str]:
    """The running gateway's profile set, from the ``AWM_PROFILES`` env
    (comma list; unset ⇒ empty ⇒ only unmarked/core services bootstrap)."""
    raw = os.environ.get("AWM_PROFILES") or ""
    return {p.strip() for p in raw.split(",") if p.strip()}


def _read_profiles(folder: Path) -> list[str] | None:
    """The service's committed profile marker: the ``profiles`` list from
    ``service.toml``. ``None`` = no marker (core, enabled everywhere). A
    corrupt/malformed marker returns ``[]`` — marked-but-unmatched (disabled),
    never fail-open."""
    path = folder / PROFILE_MARKER
    if not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning("could not parse %s: %s — treating as profile-gated off",
                    path, exc)
        return []
    profiles = data.get("profiles")
    if not isinstance(profiles, list):
        log.warning("%s has no valid 'profiles' list — treating as "
                    "profile-gated off", path)
        return []
    return [str(p).strip() for p in profiles if str(p).strip()]


def _resolve_enabled(name: str, folder: Path,
                     enabled_map: dict[str, bool]) -> bool:
    """Fold the explicit enable flag and the profile gate into one verdict
    (precedence: explicit ``enabled.json`` entry > profile intersection >
    enabled)."""
    if name in enabled_map:
        return enabled_map[name]
    profiles = _read_profiles(folder)
    if profiles is None:
        return True
    return bool(set(profiles) & active_profiles())


def is_enabled(name: str) -> bool:
    """A service is enabled unless explicitly disabled in ``enabled.json`` or
    held out by an unmatched profile marker (see the module doc)."""
    return _resolve_enabled(name, services_root() / name, load_enabled())


def set_enabled(name: str, enabled: bool) -> None:
    """Persist one service's enable flag (atomic tmp-then-rename)."""
    state = load_enabled()
    state[name] = bool(enabled)
    path = _enabled_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_services() -> list[ServiceSpec]:
    """Scan the services root for subdirs containing an executable ``run.sh``.

    Returned sorted by name. The enable flag is folded in so callers
    (bootstrap, ``awm services list``) get one consistent view.
    """
    root = services_root()
    if not root.is_dir():
        log.warning("services root %s does not exist", root)
        return []
    enabled_map = load_enabled()
    specs: list[ServiceSpec] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        run = entry / RUN_SCRIPT
        if not run.is_file():
            continue
        specs.append(ServiceSpec(
            name=entry.name,
            cwd=str(entry),
            enabled=_resolve_enabled(entry.name, entry, enabled_map),
        ))
    return specs


def discover_service(name: str) -> ServiceSpec | None:
    """Return the spec for one service folder, or ``None`` if it has no
    ``run.sh``."""
    for spec in discover_services():
        if spec.name == name:
            return spec
    return None
