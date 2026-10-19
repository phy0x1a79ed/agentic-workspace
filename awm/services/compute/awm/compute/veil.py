"""Which processes a peer caller may see: none that run in, or name, a privately marked path."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

from awm.config import privacy

#: A path ends where the next character could not continue its last name.
_PATH_END = r"(?![\w.\-])"


def _naming(paths: set[str]) -> Callable[[str], bool]:
    if not paths:
        return lambda _text: False
    rx = re.compile("|".join(re.escape(p) + _PATH_END for p in sorted(paths, key=len, reverse=True)))
    return lambda text: bool(text) and rx.search(text) is not None


def names_marked(as_: str | None) -> Callable[[str], bool] | None:
    """For a peer caller, the test that a text names a marked path; ``None`` for a local caller.

    An unreadable marks file makes every text count as naming one.
    """
    if not privacy.from_peer(as_):
        return None
    try:
        marks = privacy.marks()
    except privacy.MarksUnreadable:
        return lambda _text: True
    paths = set()
    for m in marks:
        paths.add(os.path.normpath(m["path"]))
        paths.add(os.path.realpath(m["path"]))
    return _naming(paths)


def process_cwd(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def process_hidden(pid: int, cmdline: str, names: Callable[[str], bool]) -> bool:
    """Whether a process runs under a marked path or names one on its command line."""
    cwd = process_cwd(pid)
    return names(cmdline) or bool(cwd) and privacy.hidden_path(cwd)


def decision_hidden(row: dict[str, Any], names: Callable[[str], bool]) -> bool:
    """Whether a ledger row's command line or detail names a marked path."""
    detail = row.get("detail")
    return names(row.get("cmdline") or "") or names(json.dumps(detail, ensure_ascii=False) if detail else "")
