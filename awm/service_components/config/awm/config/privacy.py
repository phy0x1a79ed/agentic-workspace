"""Private marks: projects and scopes this node hides from every peer caller.

A mark names a project, or one scope of it, and the absolute path it covers.
The marks live in ``AWM_DIR/private.json`` as ``{"version": 1, "marks": [...]}``;
the scopes service writes the file and every process reads it, rereading when
the file changes. A caller stamped ``peer`` or ``peer:<node>`` sees nothing a
mark covers. Every other caller, local or ``user:*``, sees everything.

A file that exists but does not parse fails closed: each test below then answers
"hidden" for anything it is asked about, and ``visible_rows`` gives a peer nothing.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterable

from awm import config as _cfg

FILENAME = "private.json"
VERSION = 1

#: Row keys whose value is a filesystem path a mark can cover.
PATH_KEYS = ("path", "cwd", "worktree", "worktree_path", "repo_path", "dir")
#: Argument keys whose absolute-path value a mark can cover.
ARG_PATH_KEYS = ("scope", "path", "cwd", "worktree")


class MarksUnreadable(ValueError):
    """The marks file exists but is not a valid marks document."""


_lock = threading.Lock()
# (file key, marks) where marks is None for an unreadable file.
_cache: tuple[Any, list[dict] | None] = (None, [])


def marks_path() -> Path:
    """The marks file on this node."""
    return Path(_cfg.AWM_DIR) / FILENAME


def from_peer(as_: str | None) -> bool:
    """Whether the caller stamp names another node: bare ``peer`` or ``peer:<node>``."""
    if not isinstance(as_, str):
        return False
    stamp = as_.strip().lower()
    return stamp == "peer" or stamp.startswith("peer:")


def _parse(raw: str) -> list[dict]:
    doc = json.loads(raw)
    if not isinstance(doc, dict) or doc.get("version") != VERSION:
        raise MarksUnreadable("not a version-1 marks document")
    entries = doc.get("marks")
    if not isinstance(entries, list):
        raise MarksUnreadable("marks is not a list")
    return [_check_entry(e) for e in entries]


def _check_entry(entry: Any) -> dict:
    if not isinstance(entry, dict):
        raise MarksUnreadable(f"mark is not an object: {entry!r}")
    project, scope, path = entry.get("project"), entry.get("scope"), entry.get("path")
    if not isinstance(project, str) or not project:
        raise MarksUnreadable(f"mark has no project: {entry!r}")
    if scope is not None and (not isinstance(scope, str) or not scope):
        raise MarksUnreadable(f"mark has a malformed scope: {entry!r}")
    if not isinstance(path, str) or not os.path.isabs(path):
        raise MarksUnreadable(f"mark has no absolute path: {entry!r}")
    return {"project": project, "scope": scope, "path": path}


def _with_roots(entry: dict) -> dict:
    roots = tuple(_forms(entry["path"]))
    return {**entry, "_roots": roots, "_slugs": tuple(slug(r) for r in roots)}


def _public(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if not k.startswith("_")}


def _load() -> list[dict] | None:
    global _cache
    path = marks_path()
    try:
        st = path.stat()
    except FileNotFoundError:
        return []
    except OSError:
        return None
    key = (str(path), st.st_ino, st.st_mtime_ns, st.st_size)
    with _lock:
        if _cache[0] == key:
            return _cache[1]
        try:
            loaded: list[dict] | None = [_with_roots(e) for e in _parse(path.read_text())]
        except (OSError, ValueError):
            loaded = None
        _cache = (key, loaded)
        return loaded


def marks() -> list[dict]:
    """The marks in force; raises :class:`MarksUnreadable` when the file is broken."""
    loaded = _load()
    if loaded is None:
        raise MarksUnreadable(f"{marks_path()} exists but is not a valid marks file")
    return [_public(m) for m in loaded]


def write_marks(entries: Iterable[dict]) -> list[dict]:
    """Replace the marks file atomically with ``entries`` and return them as written."""
    checked = [_check_entry(e) for e in entries]
    path = marks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"version": VERSION, "marks": checked}, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{FILENAME}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return checked


def _scope_covers(mark_scope: str, scope: str) -> bool:
    return scope == mark_scope or scope.startswith(mark_scope + "/")


def canonical(project: str | None, scope: str | None) -> tuple[str | None, str | None]:
    """The ``(project, scope)`` a journal spelling names: ``task:<p>`` and an
    empty project with scope ``user:proj:<p>`` or ``project:<p>`` all name
    project ``<p>``. The project is ``None`` when the spelling names none."""
    if not project and isinstance(scope, str):
        for channel in ("user:proj:", "project:"):
            if scope.startswith(channel):
                return scope[len(channel):] or None, None
    if isinstance(project, str) and project.startswith("task:"):
        project = project[len("task:"):]
    return project or None, scope



def hidden(project: str, scope: str | None = None) -> bool:
    """Whether a mark covers this project, or this scope of it.

    The journal spellings ``task:<p>`` and an empty project with scope
    ``user:proj:<p>`` or ``project:<p>`` all name project ``<p>``."""
    loaded = _load()
    if loaded is None:
        return True
    project, scope = canonical(project, scope)
    if project is None:
        return False
    for m in loaded:
        if m["project"] != project:
            continue
        if m["scope"] is None or (scope is not None and _scope_covers(m["scope"], scope)):
            return True
    return False


def _forms(path: str | os.PathLike) -> set[Path]:
    p = Path(os.path.expanduser(os.fspath(path)))
    if not p.is_absolute():
        p = Path(_cfg.WORKSPACE_ROOT) / p
    forms = {Path(os.path.normpath(p))}
    try:
        forms.add(p.resolve())
    except (OSError, RuntimeError):
        pass
    return forms


def _under(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def hidden_path(path: str | os.PathLike) -> bool:
    """Whether the path is a marked path or lies under one; it need not exist."""
    loaded = _load()
    if loaded is None:
        return True
    if not loaded or not os.fspath(path):
        return False
    forms = _forms(path)
    return any(_under(f, root) for m in loaded for root in m["_roots"] for f in forms)


def slug(path: str | os.PathLike) -> str:
    """Claude Code's transcript directory name for a path."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.fspath(path))


def slug_covers(prefix: str, candidate: str) -> bool:
    """Whether the slug ``candidate`` is ``prefix`` or continues it past a ``-``."""
    return candidate == prefix or candidate.startswith(prefix + "-")


def hidden_slug(name: str) -> bool:
    """Whether a Claude Code transcript slug belongs to a marked path."""
    loaded = _load()
    if loaded is None:
        return True
    return any(slug_covers(prefix, name) for m in loaded for prefix in m["_slugs"])


def _row_hidden(row: dict) -> bool:
    project, scope = row.get("project"), row.get("scope")
    project = project if isinstance(project, str) else None
    scope = scope if isinstance(scope, str) and scope else None
    if (project or scope) and hidden(project or "", scope):
        return True
    return any(isinstance(row.get(k), str) and row[k] and hidden_path(row[k])
               for k in PATH_KEYS)


def visible_rows(rows: list, as_: str | None) -> list:
    """The rows a caller with stamp ``as_`` may see: all for a local caller."""
    if not from_peer(as_):
        return rows
    if _load() is None:
        return []
    return [r for r in rows if not (isinstance(r, dict) and _row_hidden(r))]


def hides_args(args: Any) -> bool:
    """Whether call arguments name a covered project, scope or absolute path."""
    if not isinstance(args, dict):
        return False
    project = args.get("project")
    scope = args.get("scope")
    project = project if isinstance(project, str) else ""
    named_scope = scope if isinstance(scope, str) and scope and not _looks_absolute(scope) else None
    if (project or named_scope) and hidden(project, named_scope):
        return True
    return any(_looks_absolute(args.get(k)) and hidden_path(args[k]) for k in ARG_PATH_KEYS)


def _looks_absolute(value: Any) -> bool:
    return isinstance(value, str) and (value.startswith("/") or value.startswith("~"))
