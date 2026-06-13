"""PATH resolution for bare-binary subprocess calls (leaf copy).

Lifted from ``awm.agents._path`` so agentcore stays a leaf with no awm-side
imports. The harness CLIs (``claude``, ``opencode``) commonly live in
user-local bins that systemd's minimal PATH omits, so we search a couple of
extra locations explicitly.
"""

from __future__ import annotations

import os
import shutil

_EXTRA_PATHS = (
    "/home/linuxbrew/.linuxbrew/bin",
    "/home/tony/.local/bin",
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/.opencode/bin"),
)


def resolve_bin(name: str) -> str:
    extended = os.pathsep.join((*_EXTRA_PATHS, os.environ.get("PATH", "")))
    found = shutil.which(name, path=extended)
    if not found:
        raise RuntimeError(
            f"binary {name!r} not found on PATH (searched: {extended})"
        )
    return found
