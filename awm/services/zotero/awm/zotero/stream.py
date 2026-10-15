"""Being told when the library moves, instead of asking.

Zotero runs an event stream for exactly this. A client subscribes with an API
key and is pushed a frame naming the library and its new version whenever one
changes, which happens about three seconds after somebody saves a paper on any
machine signed into the account. Between changes it costs an idle socket.

**The protocol, as observed rather than as documented.** On connect the server
sends `connected` carrying a `retry` in milliseconds, which is its own advice on
how long to wait before reconnecting. The client then sends one
`createSubscriptions` naming the key, and the server answers
`subscriptionsCreated` listing the topics it signed you up for — on this account
the personal library, its publications feed, and each group. Thereafter a change
arrives as `topicUpdated` with a topic and a version. A topic is the library id
with a leading slash, so the mapping back is a strip and nothing more. The
publications feed is a topic this mirror does not read, and is ignored by the
same rule that ignores anything that is not a library.

**A socket that looks healthy is not the same as a socket that is delivering.**
This is the failure this workspace has hit three times in other services: the
connection stays open, keepalives pass, and the subscription behind it is gone.
Keepalives only prove the far end is alive. So there are three guards, and the
third is the one that matters:

- the library's own ping and pong, which catch a peer that has stopped answering
- an idle deadline, because a stream that has said nothing for a long time is
  more likely broken than the library is quiet
- a periodic full sync underneath, which is not part of this module at all

The last one is why nothing here has to be perfect. A stream that goes deaf
costs staleness until the next tick of the floor, not a stopped mirror.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from typing import Any, Awaitable, Callable

import websockets

from awm.zotero import source

log = logging.getLogger("awm.zotero.stream")

URL = os.environ.get("ZOTERO_STREAM_URL", "wss://stream.zotero.org/")

ENABLED = os.environ.get("ZOTERO_STREAM_ENABLED", "1") not in ("0", "false", "no")

#: How long a silent stream is allowed to stay connected before it is rebuilt.
#: Long, because silence is the ordinary state: nobody saves a paper most hours.
#: Short enough that a subscription lost behind a healthy socket is repaired
#: within one, rather than never.
IDLE_S = float(os.environ.get("ZOTERO_STREAM_IDLE_S", "3600"))

#: Reconnect delay when the server has not told us its own. It always does, in
#: the `connected` frame, so this is only for a connection that never got there.
RETRY_S = float(os.environ.get("ZOTERO_STREAM_RETRY_S", "10"))

#: The ceiling on backoff after repeated failures.
MAX_RETRY_S = float(os.environ.get("ZOTERO_STREAM_MAX_RETRY_S", "300"))


def library_of(topic: str) -> str:
    """The library a topic names, or empty for a topic that is not one.

    `/users/5331043` is a library. `/users/5331043/publications` is a feed this
    mirror does not read, and anything else is something the service added after
    this was written. Both answer empty rather than being guessed at.
    """
    parts = [p for p in (topic or "").split("/") if p]
    if len(parts) == 2 and parts[0] in ("users", "groups"):
        # Named, not addressed. A topic carries the account's number and the
        # rest of this service calls the personal library `users/0`, so
        # reporting the topic verbatim would give the same library two names.
        return source.name_of(f"{parts[0]}/{parts[1]}")
    return ""


class Stream:
    """One subscription to the event stream, reconnected for as long as the
    service lives.

    `on_change` is called with a library id and the version the service says it
    is now at. It is deliberately not given the frame: everything else in it is
    either the key, which the caller already has, or a topic this does not read.
    """

    def __init__(self, on_change: Callable[[str, int], Awaitable[None]]) -> None:
        self._on_change = on_change
        self.topics: list[str] = []
        self.connected_at: float | None = None
        self.last_frame_at: float | None = None
        self.last_change_at: float | None = None
        self.changes = 0
        self.connections = 0
        self.last_error: str | None = None

    @property
    def health(self) -> dict[str, Any]:
        """What `status` reports. A stream is only useful if its deafness is
        visible before something urgent depends on it."""
        now = time.time()
        return {
            "enabled": ENABLED,
            "url": URL,
            "connected": self.connected_at is not None,
            "connected_for_s": (round(now - self.connected_at, 1)
                                if self.connected_at else None),
            "silent_for_s": (round(now - self.last_frame_at, 1)
                             if self.last_frame_at else None),
            "topics": list(self.topics),
            "libraries": [t for t in (library_of(x) for x in self.topics) if t],
            "changes_seen": self.changes,
            "connections": self.connections,
            "last_change_at": self.last_change_at,
            "last_error": self.last_error,
        }

    async def _session(self) -> float:
        """One connection, held until it breaks or goes quiet. Returns the
        reconnect delay the server asked for."""
        retry = RETRY_S
        async with websockets.connect(URL, ping_interval=20,
                                      ping_timeout=20) as ws:
            self.connections += 1
            self.connected_at = time.time()
            self.last_frame_at = time.time()
            try:
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=IDLE_S)
                    except asyncio.TimeoutError:
                        # Not an error, and not necessarily broken. It is simply
                        # not worth trusting: rebuilding costs one handshake an
                        # hour and is the only thing that repairs a subscription
                        # lost behind a socket that still looks fine.
                        log.info("zotero: stream silent for %.0fs, rebuilding",
                                 IDLE_S)
                        return retry
                    self.last_frame_at = time.time()
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    event = msg.get("event")
                    if event == "connected":
                        retry = float(msg.get("retry") or RETRY_S * 1000) / 1000
                        await ws.send(json.dumps({
                            "action": "createSubscriptions",
                            "subscriptions": [{"apiKey": source.KEY}]}))
                    elif event == "subscriptionsCreated":
                        self.topics = [t for sub in msg.get("subscriptions") or []
                                       for t in sub.get("topics") or []]
                        errors = msg.get("errors") or []
                        if errors:
                            # A key that reaches nothing subscribes to nothing
                            # and then waits for ever in perfect health.
                            self.last_error = f"subscription refused: {errors}"
                            log.error("zotero: %s", self.last_error)
                        log.info("zotero: subscribed to %s",
                                 ", ".join(self.topics) or "nothing")
                    elif event == "topicUpdated":
                        library = library_of(msg.get("topic") or "")
                        if not library:
                            continue
                        self.changes += 1
                        self.last_change_at = time.time()
                        log.info("zotero: %s moved to %s", library,
                                 msg.get("version"))
                        await self._on_change(library,
                                              int(msg.get("version") or 0))
                    elif event in ("topicAdded", "topicRemoved"):
                        # A group joined or left. The next pass enumerates the
                        # libraries anyway, so this only needs re-subscribing.
                        return retry
            finally:
                self.connected_at = None

    async def run(self) -> None:
        """Never returns. A supervised loop that exits reads as a defect."""
        failures = 0
        while True:
            if not ENABLED or not source.KEY:
                await asyncio.sleep(60)
                continue
            try:
                retry = await self._session()
                self.last_error = None
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — every disconnect lands here
                failures += 1
                retry = min(RETRY_S * (2 ** min(failures, 6)), MAX_RETRY_S)
                self.last_error = f"{type(e).__name__}: {e}"[:300]
                # At info rather than warning for the first few: a dropped
                # websocket is weather, and a service that shouts about it every
                # time trains everyone to ignore the log.
                log.log(logging.WARNING if failures > 3 else logging.INFO,
                        "zotero: stream disconnected (%d in a row): %s",
                        failures, e)
            # Jittered, so a fleet of clients does not reconnect in lockstep
            # after the service restarts.
            await asyncio.sleep(retry * (0.8 + 0.4 * random.random()))
