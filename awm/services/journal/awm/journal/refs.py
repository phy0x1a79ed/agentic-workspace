"""Time and name helpers shared by the journal modules.

The timestamp converters and ``SYSTEM_REF`` match the scopes service's own, so
a post migrated from the scopes database reads back identically.
"""

from __future__ import annotations

from datetime import datetime, timezone

SYSTEM_REF = "system"


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def iso_to_ms(iso_str: str | None) -> int | None:
    if not iso_str:
        return None
    s = iso_str.strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def ms_to_iso(ms: int | None) -> str | None:
    """Unix-ms to an ISO-8601 UTC string."""
    if ms is None:
        return None
    try:
        dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return None
    return dt.isoformat()


def validate_project(name: str) -> str:
    """``name`` if it is safe to join into a path under ``projects/``."""
    if not isinstance(name, str) or not name:
        raise ValueError("project name must be a non-empty string")
    if name in (".", "..") or name.startswith("."):
        raise ValueError(f"project name cannot start with '.' (got {name!r})")
    for ch in ("/", "\\", "\x00"):
        if ch in name:
            raise ValueError(f"project name cannot contain {ch!r} (got {name!r})")
    return name


def validate_scope(scope: str) -> str:
    """``scope`` if every ``/``-separated segment is safe to join into a path."""
    if not isinstance(scope, str) or not scope:
        raise ValueError("scope name must be a non-empty string")
    if scope.startswith("/") or scope.endswith("/"):
        raise ValueError(f"scope name cannot start or end with '/' (got {scope!r})")
    for segment in scope.split("/"):
        if not segment or segment.startswith("."):
            raise ValueError(f"scope name has an empty or dot-leading segment (got {scope!r})")
        for ch in ("\\", "\x00"):
            if ch in segment:
                raise ValueError(f"scope name cannot contain {ch!r} (got {scope!r})")
    return scope
