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

TASKS_DIR = WORKSPACE_ROOT / "tasks"
TASKS_ACTIVE_DIR = WORKSPACE_ROOT / "tasks_active"
DATA_DIR = WORKSPACE_ROOT / "data"
RESULTS_DIR = WORKSPACE_ROOT / "results"
REPORTS_DIR = WORKSPACE_ROOT / "reports"
SKILLS_DIR = WORKSPACE_ROOT / "skills"
SCRIPTS_DIR = WORKSPACE_ROOT / "scripts"

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
