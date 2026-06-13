"""PATH resolution for bare-binary subprocess calls."""
from __future__ import annotations
import os
import shutil

_EXTRA_PATHS = ("/home/linuxbrew/.linuxbrew/bin", "/home/tony/.local/bin")


def resolve_bin(name: str) -> str:
    extended = os.pathsep.join((*_EXTRA_PATHS, os.environ.get("PATH", "")))
    found = shutil.which(name, path=extended)
    if not found:
        raise RuntimeError(
            f"binary {name!r} not found on PATH (searched: {extended})"
        )
    return found
