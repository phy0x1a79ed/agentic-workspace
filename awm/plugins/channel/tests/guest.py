"""An outside agent that knows only a room's link, and reads the rest from it."""

from __future__ import annotations

import httpx


class Guest:
    def __init__(self, link: str, name: str = "visitor") -> None:
        self.link = link
        self.name = name
        self.after = 0
        self.http = httpx.AsyncClient(timeout=30)

    async def how_to(self) -> str:
        resp = await self.http.get(self.link)
        resp.raise_for_status()
        return resp.text

    async def post(self, **fields) -> httpx.Response:
        return await self.http.post(f"{self.link}/send", json={"from": self.name, **fields})

    async def say(self, text: str) -> int:
        resp = await self.post(text=text)
        resp.raise_for_status()
        return resp.json()["seq"]

    async def hear(self, wait: int = 5) -> dict:
        """The host's next messages, or the end of the room. Skips my own."""
        while True:
            resp = await self.http.get(f"{self.link}/recv",
                                       params={"after": self.after, "wait": wait})
            resp.raise_for_status()
            body = resp.json()
            self.after = body["next"]
            body["messages"] = [m for m in body["messages"] if m["by"] == "host"]
            if body["messages"] or body["ended"] or not self.after:
                return body
            if not body["messages"] and resp.elapsed.total_seconds() >= wait:
                return body

    async def leave(self) -> None:
        (await self.http.post(f"{self.link}/leave")).raise_for_status()

    async def aclose(self) -> None:
        await self.http.aclose()
