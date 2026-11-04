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

1. a declared singleton homed on another node is disabled, whatever
   ``enabled.json`` says: its tool domain is in ``peer_catalog.declared_homes()``
   (``AWM_TWOFA_PEER``, ``AWM_DOMAIN_HOME_<domain>``). A second copy would spend
   the owner's one external resource, such as a Duo device or a bot login;
2. else an explicit ``enabled.json`` entry wins (both true and false — the
   operator's word, e.g. prod's deliberately-live rlm-browser);
3. else a marked service is enabled iff its profiles intersect the active set;
4. else (no ``profiles`` key — every pre-existing service, and a ``service.toml``
   that carries only other keys such as ``tier``) enabled everywhere, the
   *baseline*.

A corrupt marker reads as marked-but-unmatched (disabled + logged), never
fail-open.

Extra roots: ``AWM_SERVICE_ROOTS`` and ``AWM_PAGE_ROOTS`` (``os.pathsep`` lists,
set in the node-local ``.awm/env``) add service and page roots after the
built-in one, so a node can run folders that live outside the awm checkout. A
folder name resolves to the first root that holds it: the built-in root wins a
clash, and the shadowed folder is logged and ignored. A missing root is logged
and skipped.

Tool, tier and archive: the same ``service.toml`` may carry ``tool`` (the MCP
tool the service publishes under; the folder name when absent), ``tier``
(``"on"``, or ``"off"`` to keep the tool off the MCP surface; anything else reads
as ``"on"``) and ``[[archive]]`` tables (the search sources the service plugs
in). All are independent of ``profiles`` and never affect whether the service
runs.

Plugin manifest: a folder may instead carry ``awm-plugin.toml``, the plugin
contract the Rust gateway loads (``docs/redux/drafts/plugin-contract.md`` on the
redux-core branch). Its ``[[service]]`` entry for the folder gives ``profiles``
and ``[[service.archive]]``, and that service's first ``[[tool]]`` gives ``tool``
and ``tier``. A key the manifest leaves out falls back to ``service.toml``, so a
folder may keep its ``[[archive]]`` tables there while the search service still
reads them from that file.

Contract rules: discovery refuses a folder whose manifest breaks the plugin
contract, as the Rust loader does, so the supervisor never starts it.
``plugin_refusals`` names each refused folder and why. A manifest that does not
parse is refused. The rules are those of ``plugin-contract.md`` § Manifest:
tool and plugin names, unique tool names, reserved names only in a core root, paths inside the folder, the platform, and each
``[[needs.tool]]`` served at its major. Refusals cascade.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
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


SERVICE_ROOTS_ENV = "AWM_SERVICE_ROOTS"
PAGE_ROOTS_ENV = "AWM_PAGE_ROOTS"

# Messages already logged, so a root or clash read on every /tools call logs once.
_logged: set[str] = set()


def _log_once(msg: str) -> None:
    if msg not in _logged:
        _logged.add(msg)
        log.warning(msg)


def extra_roots(env_var: str) -> list[Path]:
    """The existing directories listed in ``env_var``, in order. An entry that
    is not a directory is logged and skipped."""
    roots: list[Path] = []
    for raw in (os.environ.get(env_var) or "").split(os.pathsep):
        raw = raw.strip()
        if not raw:
            continue
        path = Path(raw).expanduser()
        if not path.is_dir():
            _log_once(f"{env_var}: {raw} is not a directory; ignored")
            continue
        path = path.resolve()
        if path not in roots:
            roots.append(path)
    return roots


def plugins_root() -> Path | None:
    """The built-in core plugins root, ``awm/plugins`` beside ``awm/services``.

    ``None`` when ``AWM_SERVICES_DIR`` overrides the services tree, so a test's
    temp tree never picks up the checkout's plugins.
    """
    if os.environ.get("AWM_SERVICES_DIR"):
        return None
    cand = services_root().parent / "plugins"
    return cand if cand.is_dir() else None


def service_roots() -> list[Path]:
    """The built-in services root, the core plugins root, then every
    ``AWM_SERVICE_ROOTS`` entry."""
    plugins = plugins_root()
    return [services_root(), *([plugins] if plugins else []),
            *extra_roots(SERVICE_ROOTS_ENV)]


def _valid_name(name: str) -> bool:
    return bool(name) and name == Path(name).name and not name.startswith((".", "_"))


def _first_folder(roots: list[Path], name: str) -> Path | None:
    if not _valid_name(name):
        return None
    for root in roots:
        cand = root / name
        if cand.is_dir():
            return cand
    return None


def root_folders(roots: list[Path]) -> list[Path]:
    """Every folder under ``roots``, sorted by name; a name held by an earlier
    root shadows the same name in a later one."""
    found: dict[str, Path] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            if not entry.is_dir() or not _valid_name(entry.name):
                continue
            if entry.name in found:
                _log_once(f"{entry} is shadowed by {found[entry.name]}; ignored")
                continue
            found[entry.name] = entry
    return [found[n] for n in sorted(found)]


def service_folder(name: str) -> Path | None:
    """The folder of the service called ``name``: the first service root that
    holds a directory of that name, or ``None``."""
    return _first_folder(service_roots(), name)


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
PLUGIN_MANIFEST = "awm-plugin.toml"


def active_profiles() -> set[str]:
    """The running gateway's profile set, from the ``AWM_PROFILES`` env
    (comma list; unset ⇒ empty ⇒ only unmarked baseline services bootstrap)."""
    raw = os.environ.get("AWM_PROFILES") or ""
    return {p.strip() for p in raw.split(",") if p.strip()}


def _read_profiles(folder: Path) -> list[str] | None:
    """The service's committed profile marker: the ``profiles`` list from
    ``awm-plugin.toml``'s service entry, else from ``service.toml``. ``None`` =
    not profile-gated (baseline, enabled everywhere): no file, or no
    ``profiles`` key. A file that does not parse, or whose ``profiles`` is not
    a list, returns ``[]`` — marked-but-unmatched (disabled), never fail-open."""
    data: dict | None = None
    plugin = folder / PLUGIN_MANIFEST
    if plugin.is_file():
        try:
            entry = plugin_service(folder, _load_toml(plugin))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            log.warning("could not parse %s: %s — treating as profile-gated off",
                        plugin, exc)
            return []
        if entry is not None and "profiles" in entry:
            data = entry
    path = folder / PROFILE_MARKER
    if data is None:
        if not path.is_file():
            return None
        try:
            data = _load_toml(path)
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
# Tool and tier (committed service.toml ``tool`` and ``tier`` keys)
# ---------------------------------------------------------------------------

TIER_ON = "on"
TIER_OFF = "off"

# (path, mtime_ns, size) -> parsed service.toml. /tools is fetched on every
# client tool-list, so a service.toml is re-read only when it changes.
_manifest_cache: dict[tuple[str, int, int], dict] = {}


def _load_toml(path: Path) -> dict:
    """``path`` parsed, cached on its mtime and size. Raises when it is
    missing or does not parse."""
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    cached = _manifest_cache.get(key)
    if cached is None:
        cached = tomllib.loads(path.read_text(encoding="utf-8"))
        _manifest_cache[key] = cached
    return cached


def plugin_service(folder: Path, manifest: dict) -> dict | None:
    """The ``[[service]]`` entry of a plugin manifest that runs from ``folder``:
    the one named after the folder, else the one whose ``path`` is ``"."``,
    else the only one."""
    services = [s for s in manifest.get("service") or [] if isinstance(s, dict)]
    for svc in services:
        if svc.get("name") == folder.name:
            return svc
    here = [s for s in services if (s.get("path") or ".") in (".", "./")]
    if len(here) == 1:
        return here[0]
    return services[0] if len(services) == 1 else None


def _plugin_keys(folder: Path, manifest: dict) -> dict:
    """The ``service.toml``-shaped keys a plugin manifest gives ``folder``."""
    svc = plugin_service(folder, manifest)
    if svc is None:
        return {}
    name = svc.get("name")
    tools = [t for t in manifest.get("tool") or [] if isinstance(t, dict)]
    mine = [t for t in tools if t.get("service") == name]
    if not mine and len(manifest.get("service") or []) == 1:
        mine = [t for t in tools if "service" not in t]
    out: dict = {}
    if mine:
        for key in ("tool", "tier"):
            value = mine[0].get("name" if key == "tool" else key)
            if value is not None:
                out[key] = value
    for key, src in (("profiles", "profiles"), ("archive", "archive")):
        if src in svc:
            out[key] = svc[src]
    return out


def _read_manifest(folder: Path) -> dict:
    """The surface keys of ``folder``: its ``service.toml``, overlaid with what
    its ``awm-plugin.toml`` says. Empty when neither exists or parses."""
    data: dict = {}
    for name in (PROFILE_MARKER, PLUGIN_MANIFEST):
        path = folder / name
        if not path.is_file():
            continue
        try:
            parsed = _load_toml(path)
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            _log_once(f"could not read {path}: {exc} — defaults")
            continue
        data = {**data, **(parsed if name == PROFILE_MARKER else _plugin_keys(folder, parsed))}
    return data


def _read_surface(folder: Path) -> tuple[str | None, str]:
    data = _read_manifest(folder)
    tool: str | None = None
    tier = TIER_ON
    value = data.get("tool")
    if isinstance(value, str) and value.strip():
        tool = value.strip()
    value = data.get("tier")
    if isinstance(value, str) and value.strip().lower() == TIER_OFF:
        tier = TIER_OFF
    return tool, tier


def read_tier(folder: Path) -> str:
    """The service folder's tier: ``"off"`` only when ``service.toml`` says
    ``tier = "off"``. Anything else, the legacy ``core`` and ``more`` included,
    a missing file or a file that does not parse, is ``"on"``: a broken manifest
    never switches a tool off."""
    return _read_surface(folder)[1]


def read_tool(folder: Path, default: str | None = None) -> str:
    """The tool the service folder publishes under: the ``tool`` key of its
    ``service.toml``, else ``default``, else the folder name."""
    return _read_surface(folder)[0] or default or folder.name


def read_archive(folder: Path) -> list[dict]:
    """The ``[[archive]]`` tables of the service folder's ``service.toml``: one
    ``{name, mode, ...}`` dict per search source it plugs in. A table without a
    string ``name`` is dropped."""
    tables = _read_manifest(folder).get("archive")
    if not isinstance(tables, list):
        return []
    return [dict(t) for t in tables
            if isinstance(t, dict) and isinstance(t.get("name"), str) and t["name"].strip()]


def service_archive(name: str) -> list[dict]:
    """Archive tables of the service folder called ``name``."""
    folder = service_folder(name)
    return read_archive(folder) if folder is not None else []


def archive_sources() -> list[str]:
    """Every archive source name any discovered service declares, in folder order."""
    seen: list[str] = []
    for entry in root_folders(service_roots()):
        for table in read_archive(entry):
            if table["name"] not in seen:
                seen.append(table["name"])
    return seen


def service_tier(name: str) -> str:
    """Tier of the service folder called ``name``."""
    folder = service_folder(name)
    return read_tier(folder) if folder is not None else TIER_ON


def service_tool(name: str) -> str:
    """Tool of the service folder called ``name``; ``name`` itself when no
    service root holds such a folder."""
    folder = service_folder(name)
    return read_tool(folder, name) if folder is not None else name


def singleton_owner(name: str, folder: Path | None = None) -> str | None:
    """The peer a declared singleton service lives on, or ``None`` when the
    service may run on this node."""
    from awm.gateway import peer_catalog

    if folder is None:
        folder = service_folder(name) or services_root() / name
    return peer_catalog.declared_homes().get(read_tool(folder, name))


def _resolve_enabled(name: str, folder: Path,
                     enabled_map: dict[str, bool]) -> bool:
    """Fold the singleton home, the explicit enable flag and the profile gate
    into one verdict (precedence: homed elsewhere > explicit ``enabled.json``
    entry > profile intersection > enabled)."""
    if singleton_owner(name, folder) is not None:
        return False
    if name in enabled_map:
        return enabled_map[name]
    profiles = _read_profiles(folder)
    if profiles is None:
        return True
    return bool(set(profiles) & active_profiles())


def is_enabled(name: str) -> bool:
    """A service is enabled unless homed on another node, explicitly disabled
    in ``enabled.json`` or held out by an unmatched profile marker (see the
    module doc)."""
    folder = service_folder(name) or services_root() / name
    return _resolve_enabled(name, folder, load_enabled())


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

# ---------------------------------------------------------------------------
# Contract rules, checked at load
# ---------------------------------------------------------------------------

#: Tool names only a core plugin may claim. The edge
#: list's singleton tools join them (``peer_catalog.declared_homes``).
RESERVED_TOOLS = frozenset({"awm", "2fa", "approver", "queue-store", "journal"})
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")
_MANIFEST_KEYS = {
    "": {"plugin", "service", "tool", "page", "needs"},
    "plugin": {"name", "version", "refresh"},
    "service": {"name", "path", "run", "install", "profiles", "archive"},
    "tool": {"name", "major", "service", "services", "tier", "implements"},
    "page": {"name", "dir", "entry"},
    "needs": {"secrets", "network", "tool", "contract", "platform", "gpu"},
    "need": {"name", "major"},
}
_OS = {"linux": "linux", "darwin": "macos", "win32": "windows"}


def _core_roots() -> list[Path]:
    plugins = plugins_root()
    return [services_root(), *([plugins] if plugins else [])]


def _inside(folder: Path, rel: object) -> bool:
    if not isinstance(rel, str):
        return False
    try:
        return (folder / rel).resolve().is_relative_to(folder.resolve())
    except OSError:
        return False


def _unknown(table: object, kind: str) -> list[str]:
    if not isinstance(table, dict):
        return [f"[{kind}] is not a table"]
    extra = sorted(set(table) - _MANIFEST_KEYS[kind])
    return [f"unknown key {k!r} in {'[' + kind + ']' if kind else 'the manifest'}" for k in extra]


def check_manifest(folder: Path, manifest: dict, *, core: bool) -> tuple[list[tuple[str, int]], list[str]]:
    """The tools a manifest claims, as ``(name, major)``, and every rule it breaks.

    The rules that need the whole tree, unique names and needs, are
    ``plugin_refusals``'s."""
    problems = _unknown(manifest, "")
    plugin = manifest.get("plugin")
    problems += _unknown(plugin, "plugin") if plugin is not None else ["no [plugin] table"]
    plugin = plugin if isinstance(plugin, dict) else {}
    name = plugin.get("name")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        problems.append(f"plugin name {name!r} must be letters, digits and hyphens")
    services = manifest.get("service") or []
    for svc in services:
        problems += _unknown(svc, "service")
        if isinstance(svc, dict) and not _inside(folder, svc.get("path", ".")):
            problems.append(f"service {svc.get('name')!r}: path {svc.get('path')!r} leaves the plugin folder")
    names = {svc.get("name") for svc in services if isinstance(svc, dict)}
    tools: list[tuple[str, int]] = []
    for tool in manifest.get("tool") or []:
        problems += _unknown(tool, "tool")
        if not isinstance(tool, dict):
            continue
        tname, major = tool.get("name"), tool.get("major", 1)
        if not isinstance(tname, str) or not _NAME.fullmatch(tname):
            problems.append(f"tool {tname!r} must be letters, digits and hyphens")
            continue
        if not isinstance(major, int):
            problems.append(f"tool {tname!r}: major {major!r} is not an integer")
        if any(t == tname for t, _ in tools):
            problems.append(f"tool {tname!r} is declared twice")
        if tname in reserved_tools() and not core:
            problems.append(f"tool {tname!r} is reserved for core plugins")
        named = tool.get("services") or ([tool["service"]] if "service" in tool else [])
        if "service" in tool and "services" in tool:
            problems.append(f"tool {tname!r} sets both service and services")
        if not set(named) <= names:
            problems.append(f"tool {tname!r} names a service the plugin does not declare")
        if not named and len(names) != 1:
            problems.append(f"tool {tname!r} names no service and the plugin has {len(names)}")
        for raw in tool.get("implements") or []:
            c, _, m = str(raw).partition("@")
            if not _NAME.fullmatch(c) or not m.isdigit():
                problems.append(f"tool {tname!r}: implements {raw!r} is not <contract>@<major>")
        tools.append((tname, major if isinstance(major, int) else 1))
    for page in manifest.get("page") or []:
        problems += _unknown(page, "page")
        if isinstance(page, dict) and "dir" in page and not _inside(folder, page["dir"]):
            problems.append(f"page {page.get('name')!r}: dir {page['dir']!r} leaves the plugin folder")
    needs = manifest.get("needs") or {}
    problems += _unknown(needs, "needs")
    if isinstance(needs, dict):
        for dep in (needs.get("tool") or []) + (needs.get("contract") or []):
            problems += _unknown(dep, "need")
            if isinstance(dep, dict) and not (isinstance(dep.get("name"), str) and _NAME.fullmatch(dep["name"])):
                problems.append(f"a need names {dep.get('name')!r}, not a tool or contract name")
        platform = needs.get("platform") or []
        if platform and _OS.get(sys.platform, sys.platform) not in platform:
            problems.append(f"needs platform {platform}, this node is {_OS.get(sys.platform, sys.platform)}")
    return tools, problems


def reserved_tools() -> set[str]:
    """The reserved names plus the edge list's singleton tools."""
    from awm.gateway import peer_catalog

    return set(RESERVED_TOOLS) | set(peer_catalog.declared_homes())


def plugin_refusals() -> dict[str, str]:
    """``{folder name: reason}`` for every folder the contract refuses.

    Folders claim tool names in root order, core roots first, then by name, so
    the second claim of a name is the one refused. Legacy folders (``run.sh``
    and no manifest) may share a tool among themselves while they convert."""
    core = {str(r) for r in _core_roots()}
    seen: set[str] = set()
    owners: dict[str, tuple[str, bool, int]] = {}
    refused: dict[str, str] = {}
    needs: dict[str, list[dict]] = {}
    for root in service_roots():
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or not _valid_name(entry.name) or entry.name in seen:
                continue
            seen.add(entry.name)
            path = entry / PLUGIN_MANIFEST
            if not path.is_file():
                if (entry / RUN_SCRIPT).is_file():
                    tool = read_tool(entry)
                    owner = owners.get(tool)
                    if owner and not owner[1]:
                        refused[entry.name] = f"tool {tool!r} is already served by plugin {owner[0]!r}"
                        continue
                    owners.setdefault(tool, (entry.name, True, 1))
                continue
            try:
                manifest = _load_toml(path)
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
                refused[entry.name] = f"{PLUGIN_MANIFEST}: {exc}"
                continue
            tools, problems = check_manifest(entry, manifest, core=str(root) in core)
            for tname, _ in tools:
                if tname in owners:
                    problems.append(f"tool {tname!r} is already served by plugin {owners[tname][0]!r}")
            if problems:
                refused[entry.name] = "; ".join(problems)
                continue
            for tname, major in tools:
                owners[tname] = (entry.name, False, major)
            needs[entry.name] = [d for d in ((manifest.get("needs") or {}).get("tool") or []) if isinstance(d, dict)]
    from awm.gateway import peer_catalog

    elsewhere = set(peer_catalog.declared_homes())
    changed = True
    while changed:
        changed = False
        for plugin, deps in needs.items():
            if plugin in refused:
                continue
            for dep in deps:
                name, major = dep["name"], dep.get("major", 1)
                owner = owners.get(name)
                if owner is None or owner[0] in refused:
                    if name in elsewhere:
                        continue  # a singleton homed on a peer
                    refused[plugin] = f"needs tool {name!r}, which no plugin serves"
                elif owner[2] != major:
                    refused[plugin] = f"needs tool {name!r} at major {major}, plugin {owner[0]!r} serves major {owner[2]}"
                else:
                    continue
                changed = True
                break
    for name, reason in refused.items():
        _log_once(f"plugin {name} refused: {reason}")
    return refused


def name_table() -> list[dict]:
    """This node's tools: ``{name, major, plugin, implements, peer}`` for each tool of a
    folder the contract does not refuse, as the Rust gateway's ``/hub/table`` lists them.
    ``peer`` is always ``None``: this gateway keeps no peer table."""
    refused = plugin_refusals()
    rows: list[dict] = []
    for entry in root_folders(service_roots()):
        if entry.name in refused or not (entry / RUN_SCRIPT).is_file():
            continue
        path = entry / PLUGIN_MANIFEST
        if not path.is_file():
            tool = read_tool(entry)
            if not any(r["name"] == tool for r in rows):
                rows.append({"name": tool, "major": 1, "plugin": entry.name, "implements": [], "peer": None})
            continue
        for tool in _load_toml(path).get("tool") or []:
            rows.append({"name": tool["name"], "major": tool.get("major", 1), "plugin": entry.name,
                         "implements": list(tool.get("implements") or []), "peer": None})
    return rows


def discover_services() -> list[ServiceSpec]:
    """Scan the service roots for subdirs containing an executable ``run.sh``.

    Returned sorted by name. The enable flag is folded in so callers
    (bootstrap, ``awm services list``) get one consistent view.
    """
    root = services_root()
    if not root.is_dir():
        log.warning("services root %s does not exist", root)
    enabled_map = load_enabled()
    refused = plugin_refusals()
    specs: list[ServiceSpec] = []
    for entry in root_folders(service_roots()):
        run = entry / RUN_SCRIPT
        if not run.is_file() or entry.name in refused:
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


def page_roots() -> list[Path]:
    """The built-in pages root, then every ``AWM_PAGE_ROOTS`` entry."""
    return [pages_root(), *extra_roots(PAGE_ROOTS_ENV)]


def page_folders() -> list[Path]:
    """Every page folder under the page roots, sorted by name; the built-in
    root wins a name clash."""
    return root_folders(page_roots())


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
    folder = service_folder(name)
    if folder is not None and (folder / RUN_SCRIPT).is_file():
        return _resolve_enabled(name, folder, enabled_map)
    return True


def discover_pages(*, include_disabled: bool = False) -> list[PageSpec]:
    """Scan the page roots for bundles with a built ``dist/``.

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
    enabled_map = load_enabled()
    specs: list[PageSpec] = []
    for entry in page_folders():
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
