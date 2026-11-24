"""The page's conversation with a study's room seat, as a guest in the scope's channel room.

The seats service on this node holds the room and seats an agent in the scope; `seats room`
declares the seat the first time and answers the room's link, which stays the same. The relay
keeps the room's messages, so this module keeps only the links it has looked up.
"""
from __future__ import annotations

import httpx

from awm.gatewayclient import call

_links: dict[str, str] = {}


async def room(project: str, scope: str) -> str:
    key = f"{project}/{scope}"
    if key not in _links:
        r = await call("seats", "room", {"project": project, "scope": scope}, timeout=90)
        if not r.get("link"):
            raise LookupError(f"the room of {key} is not up yet; try again shortly")
        _links[key] = r["link"].rstrip("/")
    return _links[key]


async def read(project: str, scope: str, after: int, wait: float) -> dict:
    link = await room(project, scope)
    async with httpx.AsyncClient(timeout=40) as cli:
        r = await cli.get(f"{link}/recv", params={"after": after, "wait": int(min(wait, 25))})
    r.raise_for_status()
    return {"room": link.rsplit("/", 1)[1], **r.json()}


async def say(project: str, scope: str, text: str, context: dict) -> dict:
    link = await room(project, scope)
    about = " ".join(f"{k}={v}" for k, v in context.items() if v)
    async with httpx.AsyncClient(timeout=30) as cli:
        r = await cli.post(f"{link}/send", json={"from": "scopeview", "text": f"[{about}]\n{text}" if about else text})
    r.raise_for_status()
    return r.json()
