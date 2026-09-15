"""The chess arena client, pinned to this service's data and socket directories.

Import this module before anything imports `game_master`: the arena reads its
directories once, at first import.
"""

from __future__ import annotations

import hashlib
import os

from awm.config import SERVICES_DIR

STATE_DIR = SERVICES_DIR / "rlm-chess"
os.environ.setdefault("CHESS_ARENA_DATA", str(STATE_DIR / "arena"))
# One socket directory per workspace, so a sandbox and prod never adopt each other's sessions.
_WORKSPACE_TAG = hashlib.sha1(str(STATE_DIR).encode()).hexdigest()[:8]
os.environ.setdefault("CHESS_ARENA_RUNTIME", f"/tmp/chess-arena-{os.getuid()}-{_WORKSPACE_TAG}")

from coms.protocol import RpcError  # noqa: E402
from game_master import paths, saves, sessions  # noqa: E402
from game_master.seats import is_external  # noqa: E402

OWNER = "rlm-chess"
PYTHON = os.environ.get("CHESS_PYTHON") or None

__all__ = ["OWNER", "PYTHON", "RpcError", "is_external", "paths", "saves", "sessions"]
