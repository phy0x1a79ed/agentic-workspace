"""Async Chrome DevTools Protocol client for the mira API daemon.

Connects to the always-on Opera on mira (``--remote-debugging-port=9224``),
resolves a *page* target by a URL/title substring, and runs ``Runtime.evaluate``
in that page so the daemon can drive the logged-in Slack / Teams web sessions
with their own cookies + tokens. One :class:`CDPPage` owns one page's WS.

The CDP target id is unstable — it changes when the page reloads or navigates —
so the page is **re-resolved by URL match on every (re)connect**, never cached.
Calls are serialised behind a lock (the WS multiplexes events with responses);
a transport error drops the connection and the next call transparently
re-resolves + reconnects.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

log = logging.getLogger("mira_api.cdp")


class CDPError(RuntimeError):
    """A CDP transport or evaluation error."""


class CDPPage:
    """A live CDP connection to one Opera page, selected by URL/title needle."""

    def __init__(
        self,
        http_base: str,
        needle: str,
        *,
        session: aiohttp.ClientSession | None = None,
        connect_timeout: float = 15.0,
        eval_timeout: float = 30.0,
    ) -> None:
        self._http_base = http_base.rstrip("/")
        self._needle = needle.lower()
        self._session = session
        self._own_session = session is None
        self._connect_timeout = connect_timeout
        self._eval_timeout = eval_timeout
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._id = 0
        self._lock = asyncio.Lock()
        self._target_url = ""  # last resolved page url (for /health)

    @property
    def needle(self) -> str:
        return self._needle

    @property
    def target_url(self) -> str:
        return self._target_url

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._own_session = True
        return self._session

    async def _resolve_target(self) -> str:
        """Return the webSocketDebuggerUrl of the first page matching the needle."""
        sess = await self._ensure_session()
        url = f"{self._http_base}/json"
        async with sess.get(url, timeout=aiohttp.ClientTimeout(total=self._connect_timeout)) as r:
            targets = await r.json(content_type=None)
        pages = [t for t in targets
                 if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        for t in pages:
            hay = (t.get("url", "") + " " + t.get("title", "")).lower()
            if self._needle in hay:
                self._target_url = t.get("url", "")
                return t["webSocketDebuggerUrl"]
        raise CDPError(f"no page target matching {self._needle!r} on {self._http_base}")

    async def _connect(self) -> aiohttp.ClientWebSocketResponse:
        sess = await self._ensure_session()
        ws_url = await self._resolve_target()
        # max_msg_size=0 → unlimited: localConfig_v2 / message lists can be large.
        # aiohttp sends no Origin header by default, which Opera's CDP requires.
        self._ws = await sess.ws_connect(
            ws_url, max_msg_size=0,
            timeout=aiohttp.ClientTimeout(total=self._connect_timeout))
        self._id = 0
        log.info("cdp[%s] connected to %s", self._needle, self._target_url[:80])
        return self._ws

    async def _command(self, method: str, params: dict | None = None) -> dict:
        """Send one CDP command, returning its result (skips interleaved events)."""
        if self._ws is None or self._ws.closed:
            await self._connect()
        assert self._ws is not None
        self._id += 1
        mid = self._id
        await self._ws.send_str(json.dumps(
            {"id": mid, "method": method, "params": params or {}}))
        deadline = asyncio.get_event_loop().time() + self._eval_timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise CDPError(f"{method}: timed out")
            msg = await self._ws.receive(timeout=remaining)
            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                raise CDPError(f"{method}: ws closed ({msg.type})")
            if msg.type is not aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            if data.get("id") == mid:
                if "error" in data:
                    raise CDPError(f"{method}: {data['error']}")
                return data.get("result", {})

    async def evaluate(self, expression: str) -> Any:
        """Evaluate JS in the page, awaiting promises, returning the JSON value.

        Reconnects once on a transport drop (page reload changes the target).
        A JS-level exception is raised as :class:`CDPError`.
        """
        async with self._lock:
            for attempt in (1, 2):
                try:
                    result = await self._command("Runtime.evaluate", {
                        "expression": expression,
                        "returnByValue": True,
                        "awaitPromise": True,
                    })
                    break
                except CDPError:
                    if attempt == 2:
                        raise
                    log.info("cdp[%s] reconnecting after transport error", self._needle)
                    await self._close_ws()
                    await self._connect()
            if result.get("exceptionDetails"):
                det = result["exceptionDetails"]
                text = det.get("exception", {}).get("description") or det.get("text")
                raise CDPError(f"JS error: {text}")
            return (result.get("result") or {}).get("value")

    async def evaluate_json(self, expression: str) -> Any:
        """Evaluate an expression expected to return a JSON *string*, and parse it.

        Drivers return ``JSON.stringify(...)`` so large/nested results survive
        ``returnByValue`` cleanly; this parses that back to Python.
        """
        value = await self.evaluate(expression)
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return value
        return json.loads(value)

    async def healthy(self) -> bool:
        try:
            await self.evaluate("1+1")
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _close_ws(self) -> None:
        if self._ws is not None and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        self._ws = None

    async def close(self) -> None:
        await self._close_ws()
        if self._own_session and self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None
