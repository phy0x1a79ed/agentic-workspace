"""The Cache Directory Tagging marker (https://bford.info/cachedir/).

A directory holding a valid ``CACHEDIR.TAG`` opts out — of the backup walk, and
at ``projects/<p>/`` of any DVC remote. Tar, borg and restic honour the same
marker.
"""

from __future__ import annotations

from pathlib import Path

CACHEDIR_TAG = "CACHEDIR.TAG"
CACHEDIR_SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"


def is_cache_tag(path: str) -> bool:
    """Whether the file at ``path`` opens with the tag signature."""
    try:
        with open(path, "rb") as f:
            return f.read(len(CACHEDIR_SIGNATURE)) == CACHEDIR_SIGNATURE
    except OSError:
        return False


def has_cache_tag(directory: Path) -> bool:
    """Whether ``directory`` holds a valid ``CACHEDIR.TAG`` of its own."""
    tag = directory / CACHEDIR_TAG
    return tag.is_file() and not tag.is_symlink() and is_cache_tag(str(tag))
