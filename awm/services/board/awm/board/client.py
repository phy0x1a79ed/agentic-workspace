"""A client for the board's door. Depends on httpx and nothing else in awm.

The front door imports this module, so it must stay light: no store, no
Starlette, no gateway client.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

import httpx

log = logging.getLogger("awm.board.client")

#: The server sends a comment every 30 s. Silence for three beats means the
#: connection is half-open, and the stream reconnects.
STREAM_READ_TIMEOUT_S = 95.0

BACKOFF_START_S = 1.0
BACKOFF_MAX_S = 30.0


class BoardError(Exception):
    """The door answered with something other than success."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(f"board answered {status}: {message}" if message else f"board answered {status}")
        self.status = status
        self.message = message


class BoardRefused(BoardError):
    """404. The door gives one answer for a bad token, a hidden card and a missing one."""


class BoardConflict(BoardError):
    """409. The card is already held."""


def _raise_for(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        message = str(response.json().get("error", ""))
    except (ValueError, AttributeError):
        message = response.text[:200]
    if response.status_code == 404:
        raise BoardRefused(404, message)
    if response.status_code == 409:
        raise BoardConflict(409, message)
    raise BoardError(response.status_code, message)


class BoardClient:
    """One party's view of the board."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        backoff_start: float = BACKOFF_START_S,
        backoff_max: float = BACKOFF_MAX_S,
        stream_read_timeout: float = STREAM_READ_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._timeout = timeout
        self._transport = transport
        self._backoff_start = backoff_start
        self._backoff_max = backoff_max
        self._stream_read_timeout = stream_read_timeout
        self._http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.base_url,
                headers=self._headers,
                timeout=self._timeout,
                transport=self._transport,
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def __aenter__(self) -> "BoardClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _send(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._client().request(method, f"/board{path}", **kwargs)
        _raise_for(response)
        return response.json()

    # -- card verbs ----------------------------------------------------------

    async def post(
        self,
        *,
        kind: str,
        recipient: str,
        title: str,
        body: str = "",
        priority: str = "normal",
        reply_to: str | None = None,
    ) -> dict:
        payload = {"kind": kind, "recipient": recipient, "title": title, "body": body, "priority": priority}
        if reply_to:
            payload["reply_to"] = reply_to
        return await self._send("POST", "/cards", json=payload)

    async def claim(self, card_id: str) -> dict:
        return await self._send("POST", f"/cards/{card_id}/claim")

    async def complete(self, card_id: str, result: Any = None) -> dict:
        return await self._send("POST", f"/cards/{card_id}/complete", json={"result": result})

    async def fail(self, card_id: str, reason: str = "") -> dict:
        return await self._send("POST", f"/cards/{card_id}/fail", json={"reason": reason})

    async def get(self, card_id: str) -> dict:
        return await self._send("GET", f"/cards/{card_id}")

    async def list(self, **filters: str) -> list[dict]:
        params = {k: v for k, v in filters.items() if v}
        return await self._send("GET", "/cards", params=params)

    # -- the stream ----------------------------------------------------------

    async def stream(self, last_event_id: int | None = None) -> AsyncIterator[tuple[int, str, dict]]:
        """Yield ``(event_id, type, card)`` forever, reconnecting from the last id seen.

        A refused token ends the iteration with ``BoardRefused``, because
        retrying a revoked token only repeats the refusal. Anything else —
        a dropped connection, a 5xx, silence — backs off and reconnects.
        """
        cursor = last_event_id
        delay = self._backoff_start
        while True:
            headers = {"Accept": "text/event-stream"}
            if cursor is not None:
                headers["Last-Event-ID"] = str(cursor)
            timeout = httpx.Timeout(self._timeout, read=self._stream_read_timeout)
            try:
                async with self._client().stream("GET", "/board/stream", headers=headers, timeout=timeout) as response:
                    if response.status_code == 404:
                        raise BoardRefused(404, "stream refused")
                    if not response.is_success:
                        raise BoardError(response.status_code, "stream failed")
                    async for event_id, event_type, card in _parse(response.aiter_lines()):
                        delay = self._backoff_start
                        if cursor is not None and event_id <= cursor:
                            continue
                        cursor = event_id
                        yield event_id, event_type, card
                    log.info("board stream closed by the server; reconnecting")
            except BoardRefused:
                raise
            except (httpx.HTTPError, BoardError) as exc:
                log.warning("board stream dropped (%s); reconnecting in %.1fs", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._backoff_max)


async def _parse(lines: AsyncIterator[str]) -> AsyncIterator[tuple[int, str, dict]]:
    """Fold SSE lines into events. Comments and events without an id are skipped."""
    event_id: int | None = None
    event_type = "message"
    data: list[str] = []
    async for line in lines:
        if line == "":
            if event_id is not None and data:
                try:
                    yield event_id, event_type, json.loads("\n".join(data))
                except ValueError:
                    log.warning("board stream: dropped an event with bad JSON")
            event_id, event_type, data = None, "message", []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "id":
            try:
                event_id = int(value)
            except ValueError:
                event_id = None
        elif field == "event":
            event_type = value
        elif field == "data":
            data.append(value)
