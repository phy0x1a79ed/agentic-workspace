"""PATH resolution for bare-binary subprocess calls.

Systemd user units run with a minimal PATH that omits ~/.local/bin and
linuxbrew. Bare `subprocess.Popen(["claude", ...])` or
`asyncio.create_subprocess_exec("awm", ...)` then dies with
FileNotFoundError, which several call sites historically swallowed —
producing silent failures. Use `resolve_bin("claude")` to get an
absolute path that works regardless of inherited PATH.
"""

from __future__ import annotations

import os
import shutil

_EXTRA_PATHS = ("/home/linuxbrew/.linuxbrew/bin", "/home/tony/.local/bin")


def resolve_bin(name: str) -> str:
    """Return an absolute path to ``name``, searching extended PATH.

    Raises RuntimeError (not FileNotFoundError) so callers can't swallow
    it as a generic IO error — the search path is included in the message
    for debuggability.
    """
    extended = os.pathsep.join((*_EXTRA_PATHS, os.environ.get("PATH", "")))
    found = shutil.which(name, path=extended)
    if not found:
        raise RuntimeError(
            f"binary {name!r} not found on PATH (searched: {extended})"
        )
    return found
