"""Where the kb server's code, store and interpreter are.

The server is code in the `kb` project, run in that project's own `kb` env:
its dependencies never enter the awm env, whose torch pin is load-bearing.
This service only supervises it, so everything here is a path or a number.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

from awm.config import AWM_DIR, WORKSPACE_ROOT

SERVICE_DIR = Path(__file__).resolve().parents[2]

CHECKOUT = Path(os.environ.get("KB_CHECKOUT") or (WORKSPACE_ROOT / "projects" / "kb" / "release"))
LIVE = Path(os.environ.get("KB_LIVE") or (CHECKOUT / "live"))

STATE_DIR = Path(os.environ.get("KB_STATE_DIR") or (AWM_DIR / "services" / "kb"))
LOG_FILE = STATE_DIR / "logs" / "kb-server.log"

#: Written by `install.sh`: the absolute interpreter of the `kb` env, so the
#: child starts under systemd's minimal PATH, where `mamba` does not exist.
PYTHON_FILE = SERVICE_DIR / "kb-python"

HOST = "127.0.0.1"
PORT = int(os.environ.get("KB_PORT", "12531"))
URL = f"http://{HOST}:{PORT}"

#: The paper fast layer: the base kb's `kb.fastlayer` builds from semdb and ships here, and the
#: delta this service fills from Zotero.
PAPERS_BASE = Path(os.environ.get("KB_PAPERS_BASE") or (STATE_DIR / "papers.db"))
PAPERS_DELTA = Path(os.environ.get("KB_PAPERS_DELTA") or (STATE_DIR / "papers_delta.db"))
HOPS = Path(os.environ.get("KB_HOPS") or (STATE_DIR / "hops"))
#: What the kb server serves read-only: the entry and noun indexes (`entries/`, `nouns/`, each a `current` link to a
#: generation). The server finds a missing one unavailable and keeps running.
VECTORS = Path(os.environ.get("KB_VECTORS") or (STATE_DIR / "w2" / "vectors"))
ZOTERO_POLL_S = float(os.environ.get("KB_ZOTERO_POLL_S", "300"))
#: The node whose zotero service holds the library mirror; `local` for this one.
ZOTERO_PEER = os.environ.get("KB_ZOTERO_PEER", "mira")

HEALTH_INTERVAL_S = float(os.environ.get("KB_HEALTH_INTERVAL_S", "20"))
START_TIMEOUT_S = float(os.environ.get("KB_START_TIMEOUT_S", "300"))


def has_server() -> bool:
    return (CHECKOUT / "src" / "kb" / "server.py").is_file()


def python() -> str | None:
    """The `kb` env's interpreter, or None when install.sh never recorded one."""
    if os.environ.get("KB_PYTHON"):
        return os.environ["KB_PYTHON"]
    try:
        recorded = PYTHON_FILE.read_text().strip()
    except OSError:
        return None
    return recorded if recorded and Path(recorded).exists() else None


def listening(port: int | None = None, *, timeout: float = 0.5) -> bool:
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex((HOST, port or PORT)) == 0
