"""The gateway's private-mark gate: what a peer-stamped caller may not reach.

A peer caller (``peer`` or ``peer:<node>``, domestic included) never reaches a
project, scope or path this node marks private (:mod:`awm.config.privacy`).
Every door a peer can come through asks here: the catalog dispatch, the
``/svc/<svc>`` door, the emit fan-out and the static mounts. A refusal is
answered by each door as it answers a name it does not know, so a refusal does
not confirm the target exists. Local and ``user:*`` callers are never gated.

While any mark is in force, the static door also refuses a peer the places
that index or record private work wherever it lives: the AWM state directory,
Claude Code's ``~/.claude``, transcript directories named after a marked path,
and the host's pseudo and scratch filesystems. A check that raises refuses the
peer: the gate fails closed.
"""

from __future__ import annotations

import logging
import os
import pwd
from pathlib import Path
from typing import Any

from awm import config as _cfg
from awm.config import privacy

log = logging.getLogger("awm.gateway.private_gate")

# Depth to which nested argument objects and lists are searched for a path.
_MAX_DEPTH = 4


#: Host directories a peer is never served from while a mark is in force,
#: unless the workspace itself lies under one (a scratch sandbox in ``/tmp``).
SYSTEM_DENY_ROOTS = ("/proc", "/sys", "/dev", "/run", "/tmp")


def refuses(as_: str | None, args: Any) -> bool:
    """Whether a call with ``args`` from caller ``as_`` names something private."""
    if not privacy.from_peer(as_) or not isinstance(args, dict):
        return False
    try:
        return _refuses(args)
    except Exception:
        log.exception("private gate failed on a peer call; refusing it")
        return True


def _refuses(args: dict) -> bool:
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
        return _emit_hidden(payload)
    except Exception:
        log.exception("private gate failed on an emit; withholding it from peers")
        return True


def _emit_hidden(payload: Any) -> bool:
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
        return _path_refused(path)
    except Exception:
        log.exception("private gate failed on a peer file request; refusing it")
        return True


def _path_refused(path: Any) -> bool:
    try:
        if not privacy.marks():
            return False
    except privacy.MarksUnreadable:
        return True
    if privacy.hidden_path(path):
        return True
    forms = _forms(path)
    if any(_under(f, root) for f in forms for root in _record_roots()):
        return True
    workspaces = _forms(_cfg.WORKSPACE_ROOT)
    if any(_under(f, root) for f in forms for root in _system_roots()) and not any(
            _under(f, ws) for f in forms for ws in workspaces):
        return True
    return any(privacy.hidden_slug(part) for f in forms for part in f.parts)


def _forms(path: Any) -> set[Path]:
    """The path as named (normalised) and as it resolves through symlinks."""
    raw = os.path.expanduser(os.fspath(path))
    return {Path(os.path.normpath(raw)), Path(os.path.realpath(raw))}


def _under(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _record_roots() -> set[Path]:
    """The AWM state directories and every ``~/.claude`` this gateway may know."""
    roots = _forms(_cfg.AWM_DIR) | _forms(Path(_cfg.canonical_workspace()) / ".awm")
    homes = {Path.home()}
    try:
        homes.add(Path(pwd.getpwuid(os.getuid()).pw_dir))
    except KeyError:
        pass
    for home in homes:
        roots |= _forms(home / ".claude")
    return roots


def _system_roots() -> set[Path]:
    roots: set[Path] = set()
    for root in SYSTEM_DENY_ROOTS:
        roots |= _forms(root)
    return roots
