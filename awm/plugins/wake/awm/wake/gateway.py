"""Calls to other plugins, only through the gateway, by tool name (install.md R117 items 1 and 2).

A call that names a ``peer`` goes to the local ``/invoke`` too: the gateway
carries it over the ssh edge.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx


REFLECTION = "reflection"
WAKE = "wake"
#: The contract a harness plugin implements; its provider serves ``revive_pinned``.
HARNESS_CONTRACT = "harness@1"


class CallError(RuntimeError):
    pass


def _unwrap(body: Any) -> Any:
    result = body.get("result", body) if isinstance(body, dict) else body
    if isinstance(result, str):
        try:
            return json.loads(result)
        except ValueError:
            return result
    return result


class Gateway:
    def __init__(self, hub_url: str | None = None, timeout: float = 60.0):
        self.hub_url = (hub_url or os.environ.get("AWM_HUB_URL") or "http://127.0.0.1:12100").rstrip("/")
        self.timeout = timeout

    async def call(self, name: str, verb: str, args: dict | None = None,
                   peer: str | None = None) -> Any:
        payload: dict[str, Any] = {"verb": verb, "args": args or {}}
        if peer:
            payload["peer"] = peer
        from awm import gatewayclient

        headers = {}
        if token := gatewayclient.service_token():
            headers["X-Awm-Service-Token"] = token
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as cli:
                resp = await cli.post(f"{self.hub_url}/invoke",
                                      json={"name": name, "args": payload}, headers=headers)
        except httpx.HTTPError as exc:
            raise CallError(f"{name}.{verb}: gateway unreachable: {exc}") from exc
        if resp.status_code // 100 != 2:
            raise CallError(f"{name}.{verb}: HTTP {resp.status_code}: {resp.text[:300]}")
        return _unwrap(resp.json())

    async def harness(self) -> str | None:
        """The tool of this node's harness provider, or None where the gateway names none.

        The Rust gateway answers ``/hub/contract/harness@1``. Today's Python
        gateway does not, and serves ``revive_pinned`` on reflection.
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as cli:
                resp = await cli.get(f"{self.hub_url}/hub/contract/{HARNESS_CONTRACT}")
            providers = resp.json() if resp.status_code // 100 == 2 else []
        except (httpx.HTTPError, ValueError):
            return None
        local = [p for p in providers if isinstance(p, dict) and not p.get("peer") and p.get("name")]
        return str(local[0]["name"]) if local else None

    async def up(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as cli:
                resp = await cli.get(f"{self.hub_url}/status")
            return resp.status_code // 100 == 2
        except httpx.HTTPError:
            return False
