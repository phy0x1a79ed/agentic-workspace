"""Keep `/effort` and `/model` from changing the user's defaults.

In an interactive Claude Code session both commands set the running session and
then save the same value to ``~/.claude/settings.json`` as the default for every
new session: ``/effort`` under ``modelSettings.<model>.effortLevel``,
``/model`` as the top-level ``model``. (``/effort max`` is session-only and
writes nothing; there is no non-persisting form of either command a person can
type.) A session-management verb must not move the user's defaults, so the verb
snapshots those keys before the command and puts back what the command changed.

Putting back is safe for the running session. Both commands store the new value
in the session's own state first, and every lookup of effort or model prefers that
state over the settings file, so the file is read for the next session only.

Only the keys the commands write are touched. Anything else in the file, including
edits made while the verb was waiting, is left as it is, and the file keeps its
own formatting.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from awm.claudedaemon import roster

log = logging.getLogger("awm.reflection.settings_guard")

TOP_KEYS = ("effortLevel", "model")
# How long to look for the CLI's write after the command went in, how often, and
# the beat after it lands so a write in two steps is seen whole.
WRITE_WAIT_S = 4.0
POLL_S = 0.25
SETTLE_S = 0.4
# A write can land after the first restore, so the file is checked again.
RECHECK_S = 1.0
MAX_PASSES = 3

_ABSENT = object()


def settings_path() -> Path:
    return roster.claude_home() / "settings.json"


@dataclass(frozen=True)
class Snapshot:
    """The keys `/effort` and `/model` write, as they were, and the file's stamp then."""

    path: Path
    keys: dict
    stamp: tuple


def _stamp(path: Path) -> tuple:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


def _read(path: Path) -> Optional[tuple[str, dict]]:
    try:
        text = path.read_text()
        doc = json.loads(text)
    except (OSError, ValueError):
        return None
    return (text, doc) if isinstance(doc, dict) else None


def _keys_of(doc: dict) -> dict:
    return {k: doc[k] for k in (*TOP_KEYS, "modelSettings") if k in doc}


def snapshot(path: Optional[Path] = None) -> Optional[Snapshot]:
    """The defaults as they stand now, or None when the file is absent or not JSON."""
    path = path or settings_path()
    found = _read(path)
    if found is None:
        log.warning("reflection: %s is missing or not plain JSON, so the defaults "
                    "in it cannot be guarded", path)
        return None
    return Snapshot(path, json.loads(json.dumps(_keys_of(found[1]))), _stamp(path))


def _model_efforts(models: Any) -> dict:
    if not isinstance(models, dict):
        return {}
    return {m: v["effortLevel"] for m, v in models.items()
            if isinstance(v, dict) and "effortLevel" in v}


def _revert(snap: Snapshot, doc: dict) -> list[str]:
    """Put the guarded keys of ``doc`` back as the snapshot had them. Names what moved."""
    moved = []
    for key in TOP_KEYS:
        before, now = snap.keys.get(key, _ABSENT), doc.get(key, _ABSENT)
        if before != now:
            if before is _ABSENT:
                del doc[key]
            else:
                doc[key] = before
            moved.append(key)
    was = _model_efforts(snap.keys.get("modelSettings"))
    now = _model_efforts(doc.get("modelSettings"))
    for model in [m for m in {*was, *now} if was.get(m, _ABSENT) != now.get(m, _ABSENT)]:
        models = doc.get("modelSettings")
        if not isinstance(models, dict):
            models = doc["modelSettings"] = {}
        entry = models.get(model)
        if not isinstance(entry, dict):
            entry = models[model] = {}
        if model in was:
            entry["effortLevel"] = was[model]
        else:
            entry.pop("effortLevel", None)
            if not entry and model not in (snap.keys.get("modelSettings") or {}):
                del models[model]
        moved.append(f"modelSettings.{model}.effortLevel")
    if "modelSettings" in doc and not doc["modelSettings"] \
            and "modelSettings" not in snap.keys:
        del doc["modelSettings"]
    return moved


def _indent(text: str) -> Any:
    for line in text.splitlines()[1:]:
        stripped = line.lstrip(" \t")
        if stripped and stripped != line:
            return "\t" if line[0] == "\t" else len(line) - len(stripped)
    return 2


def _write(path: Path, text: str, doc: dict) -> None:
    out = json.dumps(doc, indent=_indent(text), ensure_ascii=False)
    if text.endswith("\n"):
        out += "\n"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings-guard-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(out)
        os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _restore_once(snap: Snapshot) -> list[str]:
    found = _read(snap.path)
    if found is None:
        return []
    text, doc = found
    moved = _revert(snap, doc)
    if moved:
        _write(snap.path, text, doc)
    return moved


def restore(snap: Optional[Snapshot], *, wait_s: float = WRITE_WAIT_S,
            sleep: Callable[[float], None] = time.sleep,
            clock: Callable[[], float] = time.monotonic) -> dict:
    """Wait for the CLI's write, then put the guarded keys back. Never raises.

    Returns ``restored``, the keys that were put back, and ``changed``, whether the
    file was written at all while waiting.
    """
    out: dict = {"restored": [], "changed": False}
    if snap is None:
        return out
    try:
        deadline = clock() + wait_s
        while _stamp(snap.path) == snap.stamp:
            if clock() >= deadline:
                return out
            sleep(POLL_S)
        out["changed"] = True
        sleep(SETTLE_S)
        for _ in range(MAX_PASSES):
            moved = _restore_once(snap)
            out["restored"] = sorted({*out["restored"], *moved})
            sleep(RECHECK_S)
            if not _keys_differ(snap):
                break
        if out["restored"]:
            log.info("reflection: put back %s in %s", ", ".join(out["restored"]),
                     snap.path)
    except Exception as exc:  # noqa: BLE001 - a guard must not fail the verb
        log.warning("reflection: could not guard the defaults in %s: %s",
                    snap.path, exc)
        out["error"] = str(exc)
    return out


def _keys_differ(snap: Snapshot) -> bool:
    found = _read(snap.path)
    return found is not None and bool(_revert(snap, json.loads(json.dumps(found[1]))))
