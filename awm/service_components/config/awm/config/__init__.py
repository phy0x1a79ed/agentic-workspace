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
# Derived paths
# ---------------------------------------------------------------------------

AWM_DIR = WORKSPACE_ROOT / ".awm"
DB_PATH = AWM_DIR / "state.db"
PID_FILE = AWM_DIR / "awm.pid"
LOG_FILE = AWM_DIR / "awm.log"
ENV_FILE = AWM_DIR / "env"

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
