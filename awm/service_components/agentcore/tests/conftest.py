"""Shared fixtures + CLI-availability skip helpers for agentcore tests."""

from __future__ import annotations

import shutil

import pytest


def _have(bin_name: str) -> bool:
    import os
    extra = ":".join((
        "/home/linuxbrew/.linuxbrew/bin",
        os.path.expanduser("~/.local/bin"),
        os.path.expanduser("~/.opencode/bin"),
        os.environ.get("PATH", ""),
    ))
    return shutil.which(bin_name, path=extra) is not None


HAVE_CLAUDE = _have("claude")
HAVE_OPENCODE = _have("opencode")

requires_claude = pytest.mark.skipif(
    not HAVE_CLAUDE, reason="claude CLI not installed"
)
requires_opencode = pytest.mark.skipif(
    not HAVE_OPENCODE, reason="opencode CLI not installed"
)
