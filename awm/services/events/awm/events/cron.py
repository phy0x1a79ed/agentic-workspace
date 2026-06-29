"""Stdlib-only cron / interval parsing for the events scheduler.

Two schedule forms are supported, both resolved by :func:`next_due` to "the next
epoch timestamp strictly after a given instant":

* **Interval** — ``@every <n><unit>`` (unit ``s`` / ``m`` / ``h``). Fires every
  ``n`` units relative to the previous fire; sub-minute granularity, handy for
  demos and tests. ``@every 30s`` → tick every 30 seconds.
* **Standard 5-field cron** — ``min hour dom month dow`` with ``*``, ``*/step``,
  ranges (``a-b``), and comma lists (``a,b,c``). Minute granularity, evaluated in
  local time. Standard dom/dow semantics: when *both* are restricted, a minute
  matches if *either* field matches.

No third-party dependency (croniter is not in the env). The cron walk steps
minute-by-minute from the search start and is bounded to ~366 days so an
unsatisfiable spec raises rather than looping forever.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

_INTERVAL_RE = re.compile(r"^@every\s+(\d+)\s*([smh])$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}

# (low, high) inclusive bounds per cron field position.
_FIELD_BOUNDS = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
_MAX_MINUTES = 366 * 24 * 60  # walk bound: an unsatisfiable spec errors out


class CronError(ValueError):
    """Raised when a schedule spec cannot be parsed."""


def interval_seconds(spec: str) -> int | None:
    """Return the interval in seconds if ``spec`` is an ``@every`` form, else None."""
    m = _INTERVAL_RE.match(spec.strip())
    if not m:
        return None
    n = int(m.group(1))
    if n <= 0:
        raise CronError(f"interval must be positive: {spec!r}")
    return n * _UNIT_SECONDS[m.group(2).lower()]


def _parse_field(field: str, low: int, high: int) -> set[int]:
    """Expand one cron field (``*`` / ``*/step`` / ``a-b`` / lists) to a value set."""
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"empty cron field component in {field!r}")
        step = 1
        if "/" in part:
            base, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) <= 0:
                raise CronError(f"bad step in {part!r}")
            step = int(step_s)
            part = base
        if part == "*":
            lo, hi = low, high
        elif "-" in part:
            lo_s, _, hi_s = part.partition("-")
            if not (lo_s.isdigit() and hi_s.isdigit()):
                raise CronError(f"bad range in {part!r}")
            lo, hi = int(lo_s), int(hi_s)
        elif part.isdigit():
            lo = hi = int(part)
        else:
            raise CronError(f"unrecognised cron token {part!r}")
        if lo > hi or lo < low or hi > high:
            raise CronError(f"cron value out of [{low},{high}] range: {part!r}")
        values.update(range(lo, hi + 1, step))
    return values


def _parse_cron(spec: str) -> tuple[set[int], ...]:
    """Parse a 5-field cron into per-field value sets; raise on malformed input."""
    fields = spec.split()
    if len(fields) != 5:
        raise CronError(
            f"cron must have 5 fields (min hour dom month dow), got {len(fields)}: {spec!r}")
    sets = tuple(
        _parse_field(f, low, high)
        for f, (low, high) in zip(fields, _FIELD_BOUNDS)
    )
    return sets


def _cron_matches(dt: datetime, sets: tuple[set[int], ...], raw: list[str]) -> bool:
    minute, hour, dom, month, dow = sets
    # Python weekday(): Mon=0..Sun=6 → cron Sun=0..Sat=6.
    cron_dow = (dt.weekday() + 1) % 7
    dom_restricted = raw[2] != "*"
    dow_restricted = raw[4] != "*"
    if dt.minute not in minute or dt.hour not in hour or dt.month not in month:
        return False
    if dom_restricted and dow_restricted:
        # Standard cron: either day field matching is enough.
        return dt.day in dom or cron_dow in dow
    if dom_restricted:
        return dt.day in dom
    if dow_restricted:
        return cron_dow in dow
    return True  # both wildcard


def next_due(spec: str, after: float) -> float:
    """Return the next epoch timestamp strictly greater than ``after``.

    ``spec`` is either an ``@every`` interval or a 5-field cron. Cron times are
    evaluated in local time and aligned to the minute.
    """
    spec = (spec or "").strip()
    if not spec:
        raise CronError("empty schedule spec")

    secs = interval_seconds(spec)
    if secs is not None:
        return after + secs

    sets = _parse_cron(spec)
    raw = spec.split()
    # Start at the next whole minute strictly after `after`.
    start = datetime.fromtimestamp(after).replace(second=0, microsecond=0)
    start += timedelta(minutes=1)
    cur = start
    for _ in range(_MAX_MINUTES):
        if _cron_matches(cur, sets, raw):
            return cur.timestamp()
        cur += timedelta(minutes=1)
    raise CronError(f"no matching time within a year for cron {spec!r}")


def validate(spec: str) -> None:
    """Raise :class:`CronError` if ``spec`` is not a parseable schedule."""
    # next_due exercises the full parse path; reference instant is arbitrary.
    next_due(spec, after=0.0)
