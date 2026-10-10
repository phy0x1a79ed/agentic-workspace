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
3. else (no ``profiles`` key — every pre-existing service, and a ``service.toml``
   that carries only other keys such as ``tier``) enabled everywhere, the
   *baseline*.

A corrupt marker reads as marked-but-unmatched (disabled + logged), never
fail-open.

Tier: the same ``service.toml`` may carry ``tier = "core"``. A core service's
domains are advertised on the agent-facing MCP surface; every other domain is
*discoverable* and reached through the ``more`` tool (see ``catalog``). The key
is independent of ``profiles``; absent, unknown or unreadable means
discoverable. It never affects whether the service runs.
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


# ---------------------------------------------------------------------------
# Profile gate (committed service.toml marker × AWM_PROFILES env)
# ---------------------------------------------------------------------------

PROFILE_MARKER = "service.toml"


def active_profiles() -> set[str]:
    """The running gateway's profile set, from the ``AWM_PROFILES`` env
    (comma list; unset ⇒ empty ⇒ only unmarked baseline services bootstrap)."""
    raw = os.environ.get("AWM_PROFILES") or ""
    return {p.strip() for p in raw.split(",") if p.strip()}


def _read_profiles(folder: Path) -> list[str] | None:
    """The service's committed profile marker: the ``profiles`` list from
    ``service.toml``. ``None`` = not profile-gated (baseline, enabled
    everywhere): no file, or a file with no ``profiles`` key. A file that does
    not parse, or whose ``profiles`` is not a list, returns ``[]`` —
    marked-but-unmatched (disabled), never fail-open."""
    path = folder / PROFILE_MARKER
    if not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        log.warning("could not parse %s: %s — treating as profile-gated off",
                    path, exc)
        return []
    if "profiles" not in data:
        return None
    profiles = data["profiles"]
    if not isinstance(profiles, list):
        log.warning("%s has an invalid 'profiles' value — treating as "
                    "profile-gated off", path)
        return []
    return [str(p).strip() for p in profiles if str(p).strip()]


# ---------------------------------------------------------------------------
# Tier (committed service.toml ``tier`` key)
# ---------------------------------------------------------------------------

TIER_CORE = "core"
TIER_DISCOVERABLE = "discoverable"

# (path, mtime_ns, size) -> tier. /tools is fetched on every client tool-list, so
# a service.toml is re-read only when it changes.
_tier_cache: dict[tuple[str, int, int], str] = {}


def read_tier(folder: Path) -> str:
    """The service folder's tier: ``"core"`` only when ``service.toml`` says
    ``tier = "core"``. A missing file, a missing or unknown key, or a file that
    does not parse is ``"discoverable"`` — never a crash, never core by accident."""
    path = folder / PROFILE_MARKER
    try:
        st = path.stat()
    except OSError:
        return TIER_DISCOVERABLE
    key = (str(path), st.st_mtime_ns, st.st_size)
    cached = _tier_cache.get(key)
    if cached is not None:
        return cached
    tier = TIER_DISCOVERABLE
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        value = data.get("tier")
        if isinstance(value, str) and value.strip().lower() == TIER_CORE:
            tier = TIER_CORE
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        log.warning("could not read tier from %s: %s — discoverable", path, exc)
    _tier_cache[key] = tier
    return tier


def core_services() -> list[str]:
    """Names of the service folders on disk that declare ``tier = "core"``.

    Read from the files, not from what is running: a core service that is down,
    profile-gated off on this node, or served only by a peer is still core."""
    root = services_root()
    if not root.is_dir():
        return []
    return [entry.name for entry in sorted(root.iterdir())
            if entry.is_dir() and not entry.name.startswith((".", "_"))
            and (entry / RUN_SCRIPT).is_file()
            and read_tier(entry) == TIER_CORE]


def service_tier(name: str) -> str:
    """Tier of the service folder called ``name`` under the services root."""
    if not name or name != Path(name).name or name.startswith("."):
        return TIER_DISCOVERABLE
    return read_tier(services_root() / name)


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


# ---------------------------------------------------------------------------
# Pages root resolution + discovery
# ---------------------------------------------------------------------------

def pages_root() -> Path:
    """Resolve the pages tree — the sibling of the services tree.

    Anchored to the gateway's own on-disk location exactly like
    ``services_root`` (not cwd / workspace), so the running gateway serves the
    pages that live in *its* worktree. ``AWM_PAGES_DIR`` overrides it (tests
    point this at a temp tree). The same PEP 420 namespace-package guard applies
    — ``__file__`` is ``None`` under a worktree PYTHONPATH shadow, where
    ``Path(None)`` would raise and wedge page discovery wholesale.
    """
    if env := os.environ.get("AWM_PAGES_DIR"):
        return Path(env).resolve()
    import awm.gateway
    if awm.gateway.__file__ is not None:
        pkg_dir = Path(awm.gateway.__file__).resolve().parent
    else:
        pkg_dir = Path(next(iter(awm.gateway.__path__))).resolve()
    for parent in pkg_dir.parents:
        cand = parent / "pages"
        if parent.name == "awm" and cand.is_dir():
            return cand
    # Fall back to the fixed nesting: <root>/awm/gateway/awm/gateway
    # → parents[2] == <root>/awm.
    return pkg_dir.parents[2] / "pages"


def read_prefix_txt(pkg_dir: Path, default: str) -> str:
    """A page's optional ``prefix.txt`` override (normalized to lead with
    ``/``), else ``default``.

    Shared by the CLI shadow path and boot discovery so both derive a page's
    ``/ui/...`` mount identically.
    """
    f = pkg_dir / "prefix.txt"
    if f.is_file():
        text = f.read_text(encoding="utf-8").strip()
        if text:
            return text if text.startswith("/") else "/" + text
    return default


@dataclass(frozen=True)
class PageSpec:
    """A discovered page bundle: a ``awm/pages/<name>`` folder with a built
    ``dist/``.

    ``prefix`` is the ``/ui/...`` mount and ``dist_dir`` the servable static
    root — exactly the two arguments ``registry.register_page`` needs.
    """

    name: str
    prefix: str
    dist_dir: str


def is_page_enabled(name: str, enabled_map: dict[str, bool] | None = None) -> bool:
    """A page follows an explicit ``enabled.json`` entry under its own name,
    else its same-named service's verdict, else it is enabled."""
    if enabled_map is None:
        enabled_map = load_enabled()
    if name in enabled_map:
        return enabled_map[name]
    folder = services_root() / name
    if (folder / RUN_SCRIPT).is_file():
        return _resolve_enabled(name, folder, enabled_map)
    return True


def discover_pages(*, include_disabled: bool = False) -> list[PageSpec]:
    """Scan the pages root for bundles with a built ``dist/``.

    A page is *servable* iff its ``dist/`` exists — source-only pages (no
    ``dist/`` yet, or a page still mid-build) are skipped. This mirrors how
    ``build.sh`` keys on ``index.html`` for *buildable*: a page can be
    buildable-but-not-yet-servable, which is the correct skip here. Disabled
    pages (see ``is_page_enabled``) are skipped unless ``include_disabled``.
    Returned sorted by name.
    """
    root = pages_root()
    if not root.is_dir():
        log.warning("pages root %s does not exist", root)
        return []
    enabled_map = load_enabled()
    specs: list[PageSpec] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        dist = entry / "dist"
        if not dist.is_dir():
            continue
        if not include_disabled and not is_page_enabled(entry.name, enabled_map):
            continue
        specs.append(PageSpec(
            name=entry.name,
            prefix=read_prefix_txt(entry, f"/ui/{entry.name}"),
            dist_dir=str(dist),
        ))
    return specs


def discover_page(name: str, *, include_disabled: bool = False) -> PageSpec | None:
    """Return the spec for one page bundle, or ``None`` if it has no built
    ``dist/`` (or is disabled, unless ``include_disabled``)."""
    for spec in discover_pages(include_disabled=include_disabled):
        if spec.name == name:
            return spec
    return None
