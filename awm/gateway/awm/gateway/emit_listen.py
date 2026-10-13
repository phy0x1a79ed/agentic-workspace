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
from typing import Callable, Iterable

import websockets
from websockets.exceptions import WebSocketException

RECONNECTED = {"type": "listen.reconnected"}
MAX_BACKOFF_S = 15.0


def matches(event: object, prefixes: tuple[str, ...]) -> bool:
    if not prefixes:
        return True
    kind = event.get("type") if isinstance(event, dict) else None
    return isinstance(kind, str) and kind.startswith(prefixes)


async def listen(
    url: str,
    prefixes: Iterable[str],
    write: Callable[[str], None],
    *,
    backoff_s: float = 1.0,
) -> None:
    """Print each matching frame from ``url`` as compact JSON. Never returns."""
    wanted = tuple(p for p in prefixes if p)
    connected_before = False
    delay = backoff_s
    while True:
        try:
            async with websockets.connect(url, max_size=None, open_timeout=10) as ws:
                if connected_before:
                    write(json.dumps(RECONNECTED))
                connected_before = True
                delay = backoff_s
                async for frame in ws:
                    try:
                        event = json.loads(frame)
                    except (TypeError, ValueError):
                        continue
                    if matches(event, wanted):
                        write(json.dumps(event, separators=(",", ":"), ensure_ascii=False))
        except (OSError, WebSocketException, asyncio.TimeoutError):
            pass
        await asyncio.sleep(delay)
        delay = min(delay * 2, MAX_BACKOFF_S)
