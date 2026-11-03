"""The relay's room routes, as our side uses them: the host's half."""

from __future__ import annotations

import os
from typing import Any

import httpx

#: Where rooms are served: the edge root, which carries ``/room`` beside the
#: tether's ``/tether`` mount, so a link spells out its kind (R72 item 1). The
#: bearer is the one the tether operator uses.
BASE_ENV = "AWM_ROOM_RELAY"
DEFAULT_BASE = "https://nexus.tony-xy-liu.com"
BEARER_ENV = "AWM_TETHER_ISSUE_TOKEN"

#: How long a room lasts: for one task, or until its session ends.
LASTS = ("task", "persistent")

#: How long one receive waits at the relay. The relay caps it at 25 s.
WAIT_S = 25


class RoomGone(Exception):
    """The relay answers 404 for this room: it ended long enough ago to be
    forgotten, or the relay restarted."""


class RelayError(Exception):
    """The relay refused in a way that is not about one room."""


class Rooms:
    def __init__(self, base: str | None = None, bearer: str | None = None,
                 client: httpx.AsyncClient | None = None) -> None:
        self.base = (base or os.environ.get(BASE_ENV) or DEFAULT_BASE).rstrip("/")
        self._bearer = bearer if bearer is not None else os.environ.get(BEARER_ENV, "")
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=WAIT_S + 15))

    def link(self, name: str) -> str:
        return f"{self.base}/room/{name}"

    async def open(self, lasts: str = "task") -> tuple[str, str]:
        """Open a room: its name and our host token."""
        if not self._bearer:
            raise RelayError(f"{BEARER_ENV} is not set on this node, so it cannot open a room")
        resp = await self._client.post(f"{self.base}/room", json={"kind": lasts},
                                       headers={"Authorization": f"Bearer {self._bearer}"})
        if resp.status_code == 503:
            raise RelayError("the relay has no room free right now; try again later")
        if resp.status_code != 200:
            raise RelayError(f"the relay refused to open a room ({resp.status_code}); "
                             f"check {BASE_ENV} and {BEARER_ENV}")
        body = resp.json()
        return body["name"], body["host"]

    async def send(self, name: str, token: str, message: dict[str, Any]) -> int:
        resp = await self._client.post(f"{self.base}/room/{name}/send", json=message,
                                       headers=_host(token))
        if resp.status_code == 404:
            raise RoomGone(name)
        if resp.status_code == 410:
            raise RoomGone(resp.json().get("ended") or name)
        if resp.status_code != 200:
            raise RelayError(f"the relay refused the message ({resp.status_code}): "
                             f"{resp.text.strip()}")
        return int(resp.json()["seq"])

    async def recv(self, name: str, token: str, after: int, wait: int = WAIT_S) -> dict:
        resp = await self._client.get(f"{self.base}/room/{name}/recv",
                                      params={"after": after, "wait": wait},
                                      headers=_host(token))
        if resp.status_code == 404:
            raise RoomGone(name)
        resp.raise_for_status()
        return resp.json()

    async def leave(self, name: str, token: str) -> None:
        try:
            await self._client.post(f"{self.base}/room/{name}/leave", headers=_host(token))
        except httpx.HTTPError:
            pass  # an unreachable relay ends the room itself once we go quiet

    async def aclose(self) -> None:
        await self._client.aclose()


def _host(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
