"""Path resolution, constants, and settings."""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Workspace root detection
# ---------------------------------------------------------------------------

def find_workspace_root() -> Path:
    """Walk up from CWD (or AWM_WORKSPACE env) to find the workspace root.

    The workspace root is identified by the presence of an ``AGENTS.md`` file.
    Falls back to CWD if nothing is found.
    """
    if env := os.environ.get("AWM_WORKSPACE"):
        return Path(env).resolve()

    cur = Path.cwd().resolve()
    for parent in [cur, *cur.parents]:
        if (parent / "AGENTS.md").exists():
            return parent
    return cur


WORKSPACE_ROOT = find_workspace_root()

# ---------------------------------------------------------------------------
# Canonical workspace (hub-provided on register)
# ---------------------------------------------------------------------------
# WORKSPACE_ROOT (above) is THIS process's *local* root — where its own ``.awm``
# DBs live. For a normal (native) service the local root IS the canonical
# workspace, so the two coincide and nothing changes. A shadow overlay is the
# exception: it runs with an ISOLATED local root (so its per-service DBs never
# collide with the idle base it shadows — the agents boot-reconcile closes every
# open instance row), yet its placed agents must still operate in the REAL
# workspace. The hub therefore reports its canonical workspace to every service
# on register (``ServiceRegisterResponse.canonical_workspace``); the gatewayclient
# adapter calls :func:`set_canonical_workspace` with it. A service that resolves
# where agents work (the agents service's spawned MCP env, etc.) reads
# :func:`canonical_workspace`; its DB paths stay on the local ``WORKSPACE_ROOT``.
# Until a register handshake binds it, canonical defaults to the local root, so
# tests and never-registered processes are unaffected.
_CANONICAL_WORKSPACE: Path = WORKSPACE_ROOT


def set_canonical_workspace(path: "str | Path | None") -> None:
    """Late-bind the canonical workspace the hub reported on register.

    Idempotent and tolerant of an empty value (leaves the current canonical in
    place). Distinct from ``WORKSPACE_ROOT`` on purpose — mutating that would
    repoint this process's own DBs and reintroduce the overlay↔base collision.
    """
    global _CANONICAL_WORKSPACE
    if path:
        _CANONICAL_WORKSPACE = Path(path).resolve()


def canonical_workspace() -> Path:
    """The canonical workspace (hub-provided on register), else the local root."""
    return _CANONICAL_WORKSPACE


# ---------------------------------------------------------------------------
# Derived paths
# ---------------------------------------------------------------------------

AWM_DIR = WORKSPACE_ROOT / ".awm"
DB_PATH = AWM_DIR / "state.db"
PID_FILE = AWM_DIR / "awm.pid"
LOG_FILE = AWM_DIR / "awm.log"
ENV_FILE = AWM_DIR / "env"

PROJECTS_DIR = WORKSPACE_ROOT / "projects"
# Per-task workspace units (the DAG-node execution sandbox the workspace service
# provisions) live at this top-level dir — the node-side analog of PROJECTS_DIR.
# Gitignored; one subdir per unit slug.
TASKS_DIR = WORKSPACE_ROOT / "tasks"
DATA_DIR = WORKSPACE_ROOT / "data"
SERVICES_DIR = AWM_DIR / "services"


# The skills catalog is a plain top-level dir of reference ``.md`` files
# (``<workspace_root>/skills/``) — the scopes service symlinks ``.awm/skills`` to
# it and reads its ``*.template`` files. The skills *service* (the
# ``skill_search`` / ``skill_get`` MCP tools + embeddings search) is retired; the
# files are read-only reference now, owned by no service.
SKILLS_DIR = WORKSPACE_ROOT / "skills"

GITHUB_USER = os.environ.get("AWM_GITHUB_USER", "phy0x1a79ed")

# ---------------------------------------------------------------------------
# Node identity
# ---------------------------------------------------------------------------
# Any message that leaves a node for a shared channel — the daily password push
# to Discord, an SSH lockout alert — is ambiguous in a multi-node fleet unless it
# names its sender, and the OS hostname is not that name: mira's hostname is
# ``pavilion``, so its alerts named a machine nobody calls mira. The fleet name
# is declared per node in ``<workspace>/.awm/env`` and read from there.
#
# The edge URL is declared rather than discovered for a reason worth keeping:
# capella's edge is reached at the *Windows* host's mesh address, which is
# invisible from inside WSL. That is the same asymmetry ``AWM_TLS_EXTRA_SANS``
# exists for, so the env var is the primary source and enumeration only the
# fallback for a node that owns its own mesh interface.

NODE_NAME_ENV = "AWM_NODE_NAME"
EDGE_URL_ENV = "AWM_EDGE_URL"
MESH_SUBNET_ENV = "AWM_MESH_SUBNET"
# The fleet's ZeroTier network ("phynet").
DEFAULT_MESH_SUBNET = "10.74.81.0/24"


def node_name() -> str:
    """This node's fleet name — ``AWM_NODE_NAME``, else the OS hostname."""
    import socket
    return (os.environ.get(NODE_NAME_ENV) or "").strip() or socket.gethostname()


def mesh_address() -> str | None:
    """This host's own address on the fleet mesh, or ``None`` if it has none.

    Enumerated from the interfaces this host can actually see, so it is correct
    on a node that runs its own ZeroTier client and simply absent on one that is
    reached through another host's membership.
    """
    import ipaddress
    import subprocess
    try:
        net = ipaddress.ip_network(
            os.environ.get(MESH_SUBNET_ENV) or DEFAULT_MESH_SUBNET, strict=False)
    except ValueError:
        log.warning("bad %s; ignoring", MESH_SUBNET_ENV)
        return None
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True,
                             text=True, timeout=5)
    except Exception as exc:  # noqa: BLE001 — no `hostname`, or it hung
        log.debug("mesh address enumeration failed: %s", exc)
        return None
    for tok in out.stdout.split():
        try:
            addr = ipaddress.ip_address(tok)
        except ValueError:
            continue
        if addr in net:
            return str(addr)
    return None


def edge_url() -> str | None:
    """URL of this node's own ``httpsfront`` edge, as another device reaches it.

    ``AWM_EDGE_URL`` when set (the preferred form — see the note above), else
    built from this host's enumerated mesh address and ``AWM_HTTPS_PORT``.
    ``None`` when neither is available, so a caller omits a link rather than
    printing one that goes nowhere.
    """
    if declared := (os.environ.get(EDGE_URL_ENV) or "").strip():
        return declared.rstrip("/")
    addr = mesh_address()
    if not addr:
        return None
    return f"https://{addr}:{os.environ.get('AWM_HTTPS_PORT', '8443')}"

# Sentinel project value reserved for vagrant scopes. Vagrant scopes live in
# a unified bare repo at PROJECTS_DIR / VAGRANT_PROJECT / ".bare" with one
# branch per scope, rather than belonging to a per-project repo.
VAGRANT_PROJECT = "_vagrant"


# ---------------------------------------------------------------------------
# Server settings
# ---------------------------------------------------------------------------

HOST = "127.0.0.1"
# Port is env-overridable so per-scope dev sandboxes can coexist with prod
# on the same host (dev: 7821, web-ui: 7831, …). Prod uses the default.
PORT = int(os.environ.get("AWM_PORT", "7819"))
BASE_URL = f"http://{HOST}:{PORT}"

# The shared Trilium vault's loopback port. Named here rather than in either
# service because two processes have to agree on it and neither owns it: the
# trilium supervisor binds it, and the edge proxies to it. One definition means
# there is no RPC to make, no table to cache, and nothing to go stale.
VAULT_PORT = int(os.environ.get("AWM_VAULT_PORT", "12511"))
VAULT_URL = f"http://{HOST}:{VAULT_PORT}"

IDLE_SHUTDOWN_SECONDS = int(os.environ.get("AWM_IDLE_SHUTDOWN", "1800"))  # 30 min

ACCESS_LOG = AWM_DIR / "access.log"
ALLOW_DESTRUCTIVE = os.environ.get("AWM_ALLOW_DESTRUCTIVE") == "1"


# ---------------------------------------------------------------------------
# Per-workspace env file
# ---------------------------------------------------------------------------

def load_env_file(path: Path = ENV_FILE) -> int:
    """Merge KEY=VAL lines from `path` into os.environ. Missing file → 0.

    Format: one KEY=VAL per line. Leading `export ` tolerated. Lines
    starting with `#` are comments; blank lines skipped. VAL is taken
    verbatim (no $VAR / ~ expansion); one layer of surrounding "..." or
    '...' quotes is stripped. File values OVERRIDE existing os.environ
    entries — the file is the explicit declaration.
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return 0
    except OSError as e:
        log.warning("env file %s unreadable: %s", path, e)
        return 0

    applied = 0
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            log.warning("env file %s: malformed line %d (no '=')", path, lineno)
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key or not (key[0].isalpha() or key[0] == "_") or not key.replace("_", "").isalnum():
            log.warning("env file %s: malformed line %d (bad key)", path, lineno)
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        os.environ[key] = val
        applied += 1
    if applied:
        log.info("loaded %d env vars from %s", applied, path)
    return applied
