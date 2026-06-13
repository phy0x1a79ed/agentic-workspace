"""Local timestamp helpers — re-homed from scopes.identity."""
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
    if ms is None:
        return None
    try:
        dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return None
    return dt.isoformat()
