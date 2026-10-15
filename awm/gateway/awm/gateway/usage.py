"""Rolling 60-day tally of dispatches per tool and verb.

``awm describe`` ranks verbs by it. Counts live in daily buckets in
``<AWM_DIR>/gateway/usage.json``, pruned past the window and flushed at most
every ``FLUSH_INTERVAL_S`` seconds and at exit. The first run seeds the tally
from ``usage_seed.txt`` (``<count> <tool> <verb>`` per line), counted on that
day, so the ranking is useful before the gateway has seen traffic.
"""

from __future__ import annotations

import atexit
import datetime as _dt
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Mapping

from awm import config

log = logging.getLogger("awm.gateway.usage")

WINDOW_DAYS = 60
FLUSH_INTERVAL_S = 30.0
SEED_PATH = Path(__file__).with_name("usage_seed.txt")

Key = tuple[str, str]

_lock = threading.Lock()
_days: dict[str, dict[str, int]] | None = None
_loaded_from: Path | None = None
_dirty = False
_last_flush = 0.0
_renames: Mapping[Key, Key] = {}


def storage_path() -> Path:
    return Path(config.AWM_DIR) / "gateway" / "usage.json"


def _today() -> str:
    return _dt.date.today().isoformat()


def _encode(key: Key) -> str:
    return f"{key[0]} {key[1]}"


def _decode(raw: str) -> Key | None:
    tool, _, verb = raw.partition(" ")
    return (tool, verb) if tool and verb else None


def read_seed(path: Path | None = None) -> dict[str, int]:
    """Parse a seed file. A malformed line is skipped, and so is ``describe``,
    which says nothing about which verbs matter."""
    out: dict[str, int] = {}
    try:
        lines = (path or SEED_PATH).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        parts = line.split()
        if len(parts) != 3 or not parts[0].isdigit() or parts[2] == "describe":
            continue
        key = _encode((parts[1], parts[2]))
        out[key] = out.get(key, 0) + int(parts[0])
    return out


def _prune(days: dict[str, dict[str, int]]) -> None:
    cutoff = (_dt.date.today() - _dt.timedelta(days=WINDOW_DAYS)).isoformat()
    for day in [d for d in days if d <= cutoff]:
        del days[day]


def _load() -> dict[str, dict[str, int]]:
    global _days, _loaded_from, _dirty
    path = storage_path()
    if _days is not None and _loaded_from == path:
        return _days
    days: dict[str, dict[str, int]] = {}
    seeded = False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data.get("days") or {}
        days = {d: {k: int(v) for k, v in b.items()} for d, b in raw.items()}
        seeded = bool(data.get("seeded"))
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        log.warning("could not read %s: %s — starting empty", path, exc)
        seeded = True
    _prune(days)
    _days, _loaded_from = days, path
    if not seeded:
        seed = read_seed()
        if seed:
            bucket = days.setdefault(_today(), {})
            for key, n in seed.items():
                bucket[key] = bucket.get(key, 0) + n
        _dirty = True
        _flush_locked(force=True)
    return days


def _flush_locked(*, force: bool = False) -> None:
    global _dirty, _last_flush
    if _days is None or not _dirty:
        return
    now = time.monotonic()
    if not force and now - _last_flush < FLUSH_INTERVAL_S:
        return
    path = _loaded_from or storage_path()
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _prune(_days)
        tmp.write_text(json.dumps({"seeded": True, "days": _days}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("could not write %s: %s", path, exc)
        return
    _dirty, _last_flush = False, now


def record(tool: str, verb: str) -> None:
    """Count one dispatch of ``tool`` ``verb``. Never raises."""
    global _dirty
    if not tool or not verb or verb == "describe":
        return
    try:
        with _lock:
            bucket = _load().setdefault(_today(), {})
            key = _encode((tool, verb))
            bucket[key] = bucket.get(key, 0) + 1
            _dirty = True
            _flush_locked()
    except Exception:  # noqa: BLE001 — a tally must never fail a call
        log.debug("usage tally failed", exc_info=True)


def flush() -> None:
    with _lock:
        _flush_locked(force=True)


def set_renames(renames: Mapping[Key, Key]) -> None:
    """Fold counts recorded under an old ``(tool, verb)`` into its new one when
    totalling, so history survives a rename."""
    global _renames
    _renames = dict(renames)


def totals(resolve: Callable[[Key], Key] | None = None) -> dict[Key, int]:
    """Counts per ``(tool, verb)`` over the window, renamed pairs folded in."""
    with _lock:
        days = _load()
        _prune(days)
        raw: dict[str, int] = {}
        for bucket in days.values():
            for key, n in bucket.items():
                raw[key] = raw.get(key, 0) + n
    out: dict[Key, int] = {}
    for key, n in raw.items():
        pair = _decode(key)
        if pair is None:
            continue
        pair = _renames.get(pair, pair)
        if resolve is not None:
            pair = resolve(pair)
        out[pair] = out.get(pair, 0) + n
    return out


def _reset_for_tests() -> None:
    global _days, _loaded_from, _dirty, _last_flush, _renames
    _days, _loaded_from, _dirty, _last_flush, _renames = None, None, False, 0.0, {}


atexit.register(flush)
