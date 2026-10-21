"""Turn a service's emit topic into one line per event, for a shell watcher.

An agent's harness wakes it on lines a background command prints, not on socket
frames, so this is the bridge from ``<prefix>/emit/<topic>`` to stdout. A line
is a doorbell, not the record: a reconnect can drop frames, so it prints a
``listen.reconnected`` line and the reader reconciles against the service's own
backlog verb.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, AsyncIterator, Callable, Iterable

RECONNECTED = {"type": "listen.reconnected"}
MAX_BACKOFF_S = 15.0

#: Opens one connection: ``gatewayclient.subscribe`` or ``subscribe_peer`` with
#: the service and topic bound, taking only ``on_connect``.
Stream = Callable[..., AsyncIterator[Any]]


def parse_where(text: str) -> dict[str, str]:
    """``a=1,b=2`` as ``{"a": "1", "b": "2"}``; empty text is no condition."""
    fields: dict[str, str] = {}
    for pair in (p.strip() for p in text.split(",")):
        if not pair:
            continue
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--where wants field=value, not {pair!r}")
        fields[key.strip()] = value.strip()
    return fields


def matches(event: object, prefixes: tuple[str, ...],
            where: dict[str, str] | None = None) -> bool:
    if not isinstance(event, dict):
        return not prefixes and not where
    for key, value in (where or {}).items():
        if key not in event or str(event[key]) != value:
            return False
    if not prefixes:
        return True
    kind = event.get("type")
    return isinstance(kind, str) and kind.startswith(prefixes)


async def listen(
    stream: Stream,
    prefixes: Iterable[str],
    write: Callable[[str], None],
    *,
    backoff_s: float = 1.0,
    fatal: tuple[type[Exception], ...] = (),
    where: dict[str, str] | None = None,
) -> None:
    """Print each matching event as compact JSON, reconnecting. Never returns,
    except to raise a ``fatal`` error met before the first connection."""
    wanted = tuple(p for p in prefixes if p)
    connected_before = False
    delay = backoff_s

    def on_connect() -> None:
        nonlocal connected_before, delay
        if connected_before:
            write(json.dumps(RECONNECTED))
        connected_before = True
        delay = backoff_s

    while True:
        try:
            async for event in stream(on_connect=on_connect):
                if matches(event, wanted, where):
                    write(json.dumps(event, separators=(",", ":"), ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 — every other drop is a reconnect
            if isinstance(exc, fatal) and not connected_before:
                raise
            print(f"listen: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        await asyncio.sleep(delay)
        delay = min(delay * 2, MAX_BACKOFF_S)
