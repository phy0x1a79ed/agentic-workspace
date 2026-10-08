"""Keep kb's copy of this node's scope posts current.

Two paths, because each covers the other's gap. The `posts` subscription
forwards a new post within seconds. The sweep pages every post through
`scope_fetch`, upserts them, and tells kb the full id set, so kb forgets
deleted posts and catches whatever the subscription missed while either side
was down. kb reports `posts_complete` only after a full sweep, which is the
gate `scope_fetch` relies on.

Journals are per-node, so both paths talk to the local gateway, never a peer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from awm import gatewayclient
from awm.kb import client, instances

log = logging.getLogger("awm.kb.feed")

#: The kinds the scopes service indexes for search (`search_index.INDEXED_KINDS`).
#: The two lists must agree, or `scope_fetch` routed to kb finds fewer kinds
#: than its local index would.
KINDS = ("journal", "message", "goal", "debrief", "note")
PAGE = 500
UPSERT_BATCH = 100
#: How soon a sweep reruns after a forward failed, instead of waiting the hour.
RETRY_S = 60.0

Fetch = Callable[[str, int, int], Awaitable[list[dict]]]


async def _scope_fetch(kind: str, limit: int, offset: int) -> list[dict]:
    res = await gatewayclient.call("scopes", "scope_fetch",
                                   {"kind": kind, "limit": limit, "offset": offset, "order": "desc"},
                                   timeout=60)
    return res.get("posts") or []


def to_kb(post: dict) -> dict:
    meta = post.get("meta") if isinstance(post.get("meta"), dict) else {}
    title = str(meta.get("title") or "").strip()
    body = post.get("body") or ""
    return {"id": post["id"], "project": post.get("project") or "", "scope": post.get("scope") or "",
            "kind": post.get("kind") or "", "author": post.get("author") or "", "ts": post.get("ts"),
            "body": f"{title}\n\n{body}" if title else body}


class Feed:
    def __init__(self, fetch: Fetch = _scope_fetch) -> None:
        self._fetch = fetch
        self._lock = asyncio.Lock()
        self.forwarded = 0
        self.forward_failures = 0
        self.dirty = False
        self.last_sweep: dict[str, Any] | None = None
        self.last_ok: float | None = None
        self.subscription: gatewayclient.SupervisedSubscription | None = None

    async def on_post(self, ev: Any) -> None:
        post = (ev or {}).get("post") if isinstance(ev, dict) else None
        if not post or post.get("kind") not in KINDS or not post.get("body"):
            return
        try:
            await client.upsert([to_kb(post)])
            self.forwarded += 1
        except Exception as exc:  # noqa: BLE001 — the next sweep carries it
            self.forward_failures += 1
            self.dirty = True
            log.warning("kb: forward of post %s failed (%s); a sweep will carry it", post.get("id"), exc)

    async def _all_posts(self) -> dict[str, dict]:
        posts: dict[str, dict] = {}
        for kind in KINDS:
            offset = 0
            while True:
                page = await self._fetch(kind, PAGE, offset)
                for p in page:
                    if p.get("body"):
                        posts[p["id"]] = p
                if len(page) < PAGE:
                    break
                offset += PAGE
        return posts

    async def sweep(self) -> dict[str, Any]:
        """Upsert every post, then send kb the full id set. Serialised: a second caller waits for the first."""
        async with self._lock:
            t = time.monotonic()
            posts = await self._all_posts()
            totals = {"queued": 0, "unchanged": 0, "skipped": 0}
            batch = [to_kb(p) for p in posts.values()]
            for i in range(0, len(batch), UPSERT_BATCH):
                out = await client.upsert(batch[i:i + UPSERT_BATCH])
                for k in totals:
                    totals[k] += out.get(k, 0)
            res = await client.sweep(list(posts), full=True)
            self.last_sweep = {"ts": time.time(), "posts": len(posts), **totals,
                               "missing": len(res.get("missing") or []), "forgotten": res.get("forgotten", 0),
                               "filtered": res.get("filtered", False),
                               "seconds": round(time.monotonic() - t, 2)}
            self.last_ok = time.monotonic()
            self.dirty = False
            return self.last_sweep

    def due(self) -> bool:
        if self.last_ok is None:
            return True
        waited = time.monotonic() - self.last_ok
        return waited >= instances.SWEEP_INTERVAL_S or (self.dirty and waited >= RETRY_S)

    async def loop(self, tick_s: float = RETRY_S) -> None:
        """Sweep at start once kb listens, then hourly, and sooner after a failed forward. Never exits."""
        while True:
            try:
                if self.due() and await asyncio.to_thread(instances.listening):
                    res = await self.sweep()
                    log.info("kb: sweep %s", res)
            except Exception as exc:  # noqa: BLE001 — skip the tick, never die
                self.last_sweep = {"ts": time.time(), "error": repr(exc)[:300]}
                log.warning("kb: sweep failed: %s", exc)
            await asyncio.sleep(tick_s)

    async def subscribe(self) -> None:
        self.subscription = gatewayclient.SupervisedSubscription(
            "kb/scopes.posts", lambda: gatewayclient.subscribe("scopes", "posts"), self.on_post)
        await self.subscription.run()

    def snapshot(self) -> dict[str, Any]:
        sub = self.subscription
        return {"subscription": {"healthy": sub.healthy, "events": sub.events} if sub else None,
                "forwarded": self.forwarded, "forward_failures": self.forward_failures,
                "dirty": self.dirty, "last_sweep": self.last_sweep}


FEED = Feed()
