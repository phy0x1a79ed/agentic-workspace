"""Find a command-line tool that lives in a conda env of its own.

A service runs under systemd with a minimal PATH, and tools such as dvc and
globus live in their own conda envs, so ``shutil.which`` alone finds nothing.
The lookup is not cached: a service must find a tool installed after it started.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def conda_bases() -> list[Path]:
    """The conda installs this process can see, nearest first."""
    found: list[Path] = []

    def add(base: Path) -> None:
        if (base / "conda-meta").is_dir() and base not in found:
            found.append(base)

    prefix = Path(sys.prefix)
    add(prefix.parent.parent if prefix.parent.name == "envs" else prefix)
    for var in ("AWM_CONDA_ROOT", "MAMBA_ROOT_PREFIX"):
        if value := os.environ.get(var):
            add(Path(value))
    # CONDA_EXE and MAMBA_EXE name <base>/bin/<launcher> or <base>/condabin/<launcher>.
    for var in ("CONDA_EXE", "MAMBA_EXE"):
        if value := os.environ.get(var):
            add(Path(value).parent.parent)
    return found


def tool_bin(name: str, override: str) -> str | None:
    """Absolute path to ``name``, or None when no env carries it.

    Order: the ``override`` env var, PATH, ``envs/<name>`` in each conda base,
    then the env this interpreter runs in.
    """
    if (value := os.environ.get(override)) and _executable(Path(value)):
        return value
    if found := shutil.which(name):
        return found
    candidates = [base / "envs" / name / "bin" / name for base in conda_bases()]
    candidates.append(Path(sys.prefix) / "bin" / name)
    for candidate in candidates:
        if _executable(candidate):
            return str(candidate)
    return None


def dvc_bin() -> str | None:
    return tool_bin("dvc", "AWM_DVC_BIN")
