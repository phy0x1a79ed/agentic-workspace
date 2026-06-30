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
"""

from __future__ import annotations

import json
import logging
import os
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
    # ``awm.gateway`` resolves as a regular package (``__file__`` points at its
    # ``__init__.py``) under a plain install, but as a PEP 420 *namespace*
    # package (``__file__`` is ``None``; ``__path__`` lists the dirs) when a
    # worktree shadow puts ``awm.gateway`` on more than one root via PYTHONPATH
    # — the intentional nested ``awm/gateway/awm/gateway`` layout. ``Path(None)``
    # raised ``TypeError`` in the namespace case and wedged discovery wholesale.
    # Anchor on the package directory either way.
    if awm.gateway.__file__ is not None:
        pkg_dir = Path(awm.gateway.__file__).resolve().parent
    else:
        pkg_dir = Path(next(iter(awm.gateway.__path__))).resolve()
    for parent in pkg_dir.parents:
        cand = parent / "services"
        if parent.name == "awm" and cand.is_dir():
            return cand
    # Fall back to the fixed nesting: <root>/awm/gateway/awm/gateway
    # → parents[2] == <root>/awm.
    return pkg_dir.parents[2] / "services"


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


def is_enabled(name: str) -> bool:
    """A service is enabled unless explicitly disabled in ``enabled.json``."""
    return load_enabled().get(name, True)


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
            enabled=enabled_map.get(entry.name, True),
        ))
    return specs


def discover_service(name: str) -> ServiceSpec | None:
    """Return the spec for one service folder, or ``None`` if it has no
    ``run.sh``."""
    for spec in discover_services():
        if spec.name == name:
            return spec
    return None
