"""Calls to other plugins through this node's gateway, by tool name (redux plugin contract v1.8)."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from awm.operations.relay import Refused, Unreachable

def hub() -> str:
    return os.environ.get("AWM_HUB_URL", "http://127.0.0.1:8765")


class GatewayTool:
    """One tool reached by name. Transport failures and 5xx become Unreachable."""

    def __init__(self, name: str, *, peer: str | None = None, timeout: float = 15.0) -> None:
        self.name, self.peer, self.timeout = name, peer, timeout

    def _body(self, verb: str, args: dict[str, Any], peer: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {"name": self.name, "args": {"verb": verb, "args": args}}
        peer = peer or self.peer
        if peer:
            body["args"]["peer"] = peer
        return body

    @staticmethod
    def _reply(r: httpx.Response) -> Any:
        if r.status_code >= 500 or r.status_code in (404, 408, 429):
            raise Unreachable(f"{r.status_code} {r.text[:200]}")
        if r.status_code >= 400:
            raise Refused(r.json().get("detail", r.text) if r.content else str(r.status_code))
        res = r.json().get("result")
        return json.loads(res) if isinstance(res, str) else res

    async def call(self, verb: str, args: dict[str, Any], *, timeout: float | None = None,
                   peer: str | None = None) -> Any:
        try:
            async with httpx.AsyncClient(timeout=timeout or self.timeout) as c:
                r = await c.post(f"{hub()}/invoke", json=self._body(verb, args, peer),
                                 headers={"X-Awm-Plugin": "operations"})
        except httpx.HTTPError as exc:
            raise Unreachable(str(exc)) from exc
        return self._reply(r)

    def call_sync(self, verb: str, args: dict[str, Any], *, timeout: float | None = None) -> Any:
        try:
            r = httpx.post(f"{hub()}/invoke", json=self._body(verb, args, None),
                           headers={"X-Awm-Plugin": "operations"},
                           timeout=timeout or self.timeout)
        except httpx.HTTPError as exc:
            raise Unreachable(str(exc)) from exc
        return self._reply(r)


async def usage(page: str) -> None:
    """Record one page visit (plugin contract, § Pages on their own origin)."""
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            await c.post(f"{hub()}/usage", json={"surface": "page", "plugin": "operations",
                                                 "name": page})
    except httpx.HTTPError:
        pass


class TwoFa:
    """The approver's view of 2fa on its own node (R137)."""

    def __init__(self) -> None:
        self.tool = GatewayTool("2fa", timeout=20)

    async def twofa(self, verb: str, args: dict[str, Any]) -> Any:
        return await self.tool.call(verb, args)
