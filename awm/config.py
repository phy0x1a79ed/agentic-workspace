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
# Port is env-overridable so per-scope dev sandboxes can coexist with prod
# on the same host (dev: 7821, web-ui: 7831, …). Prod uses the default.
PORT = int(os.environ.get("AWM_PORT", "7819"))
BASE_URL = f"http://{HOST}:{PORT}"

IDLE_SHUTDOWN_SECONDS = int(os.environ.get("AWM_IDLE_SHUTDOWN", "1800"))  # 30 min

ACCESS_LOG = AWM_DIR / "access.log"
ALLOW_DESTRUCTIVE = os.environ.get("AWM_ALLOW_DESTRUCTIVE") == "1"
