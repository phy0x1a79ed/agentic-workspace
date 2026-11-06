"""A Chrome DevTools connection: one browser-level websocket, page commands by flat session."""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any, Protocol

import httpx
import websockets


class CdpError(RuntimeError):
    pass


class Cdp(Protocol):
    async def send(self, method: str, params: dict | None = None, session: str | None = None) -> dict: ...


class Chrome:
    """The browser endpoint at ``base`` (``http://127.0.0.1:<port>``)."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self._ws: Any = None
        self._ids = itertools.count(1)
        self._waiting: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None

    async def connect(self) -> "Chrome":
        async with httpx.AsyncClient(timeout=10) as c:
            url = (await c.get(f"{self.base}/json/version")).json()["webSocketDebuggerUrl"]
        self._ws = await websockets.connect(url, max_size=None, open_timeout=15)
        self._reader = asyncio.create_task(self._read())
        return self

    async def _read(self) -> None:
        async for raw in self._ws:
            msg = json.loads(raw)
            fut = self._waiting.pop(msg.get("id"), None)
            if fut and not fut.done():
                fut.set_result(msg)
        for fut in self._waiting.values():
            fut.set_exception(CdpError("the browser connection closed"))

    async def send(self, method: str, params: dict | None = None, session: str | None = None) -> dict:
        mid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._waiting[mid] = fut
        frame: dict[str, Any] = {"id": mid, "method": method, "params": params or {}}
        if session:
            frame["sessionId"] = session
        await self._ws.send(json.dumps(frame))
        msg = await asyncio.wait_for(fut, 60)
        if "error" in msg:
            raise CdpError(f"{method}: {msg['error'].get('message')}")
        return msg.get("result") or {}

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
