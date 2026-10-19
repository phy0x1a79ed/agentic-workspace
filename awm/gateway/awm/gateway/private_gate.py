"""The gateway's private-mark gate: what a peer-stamped caller may not reach.

A peer caller (``peer`` or ``peer:<node>``, domestic included) never reaches a
project, scope or path this node marks private (:mod:`awm.config.privacy`).
Every door a peer can come through asks here: the catalog dispatch, the
``/svc/<svc>`` door, the emit fan-out and the static mounts. A refusal is
answered by each door as it answers a name it does not know, so a refusal does
not confirm the target exists. Local and ``user:*`` callers are never gated.
"""

from __future__ import annotations

from typing import Any

from awm.config import privacy

# Depth to which nested argument objects and lists are searched for a path.
_MAX_DEPTH = 4


def refuses(as_: str | None, args: Any) -> bool:
    """Whether a call with ``args`` from caller ``as_`` names something private."""
    if not privacy.from_peer(as_) or not isinstance(args, dict):
        return False
    try:
        if not privacy.marks():
            return False
    except privacy.MarksUnreadable:
        pass
    if privacy.hides_args(args):
        return True
    projects = args.get("projects")
    if isinstance(projects, list) and any(
            isinstance(p, str) and p and privacy.hidden(p) for p in projects):
        return True
    return any(privacy.hidden_path(p) for p in _paths_in(args, 0))


def _paths_in(value: Any, depth: int):
    """Every string in ``value`` shaped like a path: absolute, ``~`` or
    workspace-relative under ``projects/``."""
    if isinstance(value, str):
        if value.startswith(("/", "~", "projects/")):
            yield value
    elif depth < _MAX_DEPTH and isinstance(value, dict):
        for v in value.values():
            yield from _paths_in(v, depth + 1)
    elif depth < _MAX_DEPTH and isinstance(value, list):
        for v in value:
            yield from _paths_in(v, depth + 1)


def emit_hidden(payload: Any) -> bool:
    """Whether an emitted event may not reach a peer subscriber: it, or an object
    it carries one level down (``{project, scope, post: {...}}``), is covered."""
    try:
        if not privacy.marks():
            return False
    except privacy.MarksUnreadable:
        return True
    rows = [payload]
    if isinstance(payload, dict):
        rows += [v for v in payload.values() if isinstance(v, dict)]
    return len(privacy.visible_rows(rows, "peer")) < len(rows)


def path_refused(as_: str | None, path: Any) -> bool:
    """Whether a peer caller ``as_`` may not be served the file at ``path``."""
    if not privacy.from_peer(as_):
        return False
    try:
        if not privacy.marks():
            return False
    except privacy.MarksUnreadable:
        return True
    return privacy.hidden_path(path)
