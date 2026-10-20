"""Where the kb server's code, store and interpreter are.

The server is code in the `kb` project, run in that project's own `kb` env:
Cognee's dependencies never enter the awm env, whose torch pin is load-bearing.
This service only supervises it, so everything here is a path or a number.

**The store lives in the checkout it was built by**, under `live/`, beside the
code that wrote it. A snapshot copies it to `data/store/` for DVC, because a
pinned file is a read-only hardlink and the live store is written constantly.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

from awm.config import AWM_DIR, WORKSPACE_ROOT

SERVICE_DIR = Path(__file__).resolve().parents[2]

CHECKOUT = Path(os.environ.get("KB_CHECKOUT") or (WORKSPACE_ROOT / "projects" / "kb" / "release"))
LIVE = Path(os.environ.get("KB_LIVE") or (CHECKOUT / "live"))
SNAPSHOT_DIR = CHECKOUT / "data" / "store"

STATE_DIR = Path(os.environ.get("KB_STATE_DIR") or (AWM_DIR / "services" / "kb"))
LOG_FILE = STATE_DIR / "logs" / "kb-server.log"

#: Written by `install.sh`: the absolute interpreter of the `kb` env, so the
#: child starts under systemd's minimal PATH, where `mamba` does not exist.
PYTHON_FILE = SERVICE_DIR / "kb-python"

HOST = "127.0.0.1"
PORT = int(os.environ.get("KB_PORT", "12531"))
URL = f"http://{HOST}:{PORT}"

#: The Zotero mirror the zotero service keeps in the Trilium vault. kb reads it
#: for metadata and abstracts when no node holds a `ZOTERO_API_KEY`.
ZOTERO_LIBRARY = Path(os.environ.get("KB_ZOTERO_LIBRARY")
                      or (WORKSPACE_ROOT / "projects" / "trilium" / "release" / "data"
                          / "vault" / "zotero" / "library.json"))

#: The paper fast layer: the base kb's `kb.fastlayer` builds from semdb and ships here, and the
#: delta this service fills from Zotero.
PAPERS_BASE = Path(os.environ.get("KB_PAPERS_BASE") or (STATE_DIR / "papers.db"))
PAPERS_DELTA = Path(os.environ.get("KB_PAPERS_DELTA") or (STATE_DIR / "papers_delta.db"))
#: The one graph and its PageRank profiles, as the kb project's `kb.graph` builds them.
GRAPH = Path(os.environ.get("KB_GRAPH") or (STATE_DIR / "graph" / "graph.db"))
#: What the kb server serves read-only: the entry and noun indexes (`entries/`, `nouns/`, each a `current` link to a
#: generation) and the Ladybug graph. The server finds a missing one unavailable and keeps running.
VECTORS = Path(os.environ.get("KB_VECTORS") or (STATE_DIR / "w2" / "vectors"))
LADYBUG = Path(os.environ.get("KB_LADYBUG") or (STATE_DIR / "w2" / "graph" / "graph.ladybug"))
ZOTERO_POLL_S = float(os.environ.get("KB_ZOTERO_POLL_S", "300"))
#: The node whose zotero service holds the library mirror; `local` for this one.
ZOTERO_PEER = os.environ.get("KB_ZOTERO_PEER", "mira")

#: opencode's credential store, where the OpenRouter key lives on every node.
AUTH_JSON = Path(os.environ.get("KB_AUTH_JSON")
                 or (Path.home() / ".local" / "share" / "opencode" / "auth.json"))

HEALTH_INTERVAL_S = float(os.environ.get("KB_HEALTH_INTERVAL_S", "20"))
START_TIMEOUT_S = float(os.environ.get("KB_START_TIMEOUT_S", "300"))
SWEEP_INTERVAL_S = float(os.environ.get("KB_SWEEP_INTERVAL_S", "3600"))


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
