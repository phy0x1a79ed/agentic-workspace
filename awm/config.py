"""Path resolution, constants, and settings."""

from __future__ import annotations

import os
from pathlib import Path

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
# Derived paths
# ---------------------------------------------------------------------------

AWM_DIR = WORKSPACE_ROOT / ".awm"
DB_PATH = AWM_DIR / "state.db"
PID_FILE = AWM_DIR / "awm.pid"
LOG_FILE = AWM_DIR / "awm.log"

PROJECTS_DIR = WORKSPACE_ROOT / "projects"
DATA_DIR = WORKSPACE_ROOT / "data"
SKILLS_DIR = Path(__file__).resolve().parent / "skills"

GITHUB_USER = os.environ.get("AWM_GITHUB_USER", "phy0x1a79ed")

# Sentinel project value reserved for vagrant scopes. Vagrant scopes live in
# a unified bare repo at PROJECTS_DIR / VAGRANT_PROJECT / ".bare" with one
# branch per scope, rather than belonging to a per-project repo.
VAGRANT_PROJECT = "_vagrant"


# ---------------------------------------------------------------------------
# Server settings
# ---------------------------------------------------------------------------

HOST = "127.0.0.1"
PORT = 7819
BASE_URL = f"http://{HOST}:{PORT}"

IDLE_SHUTDOWN_SECONDS = int(os.environ.get("AWM_IDLE_SHUTDOWN", "1800"))  # 30 min
HEARTBEAT_INTERVAL = 30        # seconds — agents should heartbeat this often
HEARTBEAT_STALE_THRESHOLD = 120  # seconds — locks older than this are reapable
REAPER_INTERVAL = 30           # seconds — how often the reaper runs


# ---------------------------------------------------------------------------
# Exposed (network-reachable) listener
# ---------------------------------------------------------------------------

EXPOSED_HOST = os.environ.get("AWM_EXPOSED_HOST", "127.0.0.1")
EXPOSED_PORT = int(os.environ.get("AWM_EXPOSED_PORT", "7820"))
EXPOSED_PID_FILE = AWM_DIR / "awm-exposed.pid"
EXPOSED_LOG_FILE = AWM_DIR / "awm-exposed.log"

# TLS — auto-bootstrapped to $AWM_DIR/tls/{cert,key}.pem on daemon start.
# Override paths via AWM_TLS_CERT / AWM_TLS_KEY if a real cert lives elsewhere.
TLS_DIR = AWM_DIR / "tls"
TLS_CERT = Path(os.environ.get("AWM_TLS_CERT", str(TLS_DIR / "cert.pem")))
TLS_KEY = Path(os.environ.get("AWM_TLS_KEY", str(TLS_DIR / "key.pem")))

AUTH_TOKEN_ENV = "AWM_AUTH_TOKEN"
AUTH_TOKEN_FILE = Path(os.environ.get("AWM_AUTH_TOKEN_FILE", str(AWM_DIR / "auth.token")))

# Local peer identity (peer_id + advertise_url) is host-level state, like
# AUTH_TOKEN_FILE. Defaults workspace-local; override to ~/.awm/peer.json in
# a deployed unit if multiple workspaces should share an identity.
PEER_FILE = Path(os.environ.get("AWM_PEER_FILE", str(AWM_DIR / "peer.json")))

ACCESS_LOG = AWM_DIR / "access.log"
ALLOW_DESTRUCTIVE = os.environ.get("AWM_ALLOW_DESTRUCTIVE") == "1"
