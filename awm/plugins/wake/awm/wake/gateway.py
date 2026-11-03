"""Calls to other plugins, only through the gateway (install.md R117 item 1).

The tool name for each dependency comes from ``AWM_RESOLVED_TOOLS`` under the
redux gateway. Today's Python gateway sets no such file, so the legacy name is
used. A local ``/invoke`` that names a peer comes back from today's gateway as a
``peer_redirect``, and the call then goes to that peer's edge through
``awm.gatewayclient``, which is a library, not a plugin.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import httpx

PLUGIN = "wake"


def legacy_uuid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"awm:tool:{name}"))


#: The tools this plugin calls, by permanent UUID and legacy name.
REFLECTION = legacy_uuid("reflection")
WAKE = "7c1d4b8e-2f5a-4e93-a6b0-9d3e8f21c5a7"
LEGACY_NAMES = {REFLECTION: "reflection", WAKE: "wake"}


class CallError(RuntimeError):
    pass


def tool_name(tool_uuid: str) -> str:
    path = os.environ.get("AWM_RESOLVED_TOOLS")
    if path:
        try:
            resolved = json.loads(Path(path).read_text())
            return resolved[tool_uuid]["name"]
        except (OSError, ValueError, KeyError):
            pass
    return LEGACY_NAMES[tool_uuid]


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

    async def call(self, tool_uuid: str, verb: str, args: dict | None = None,
                   peer: str | None = None) -> Any:
        name = tool_name(tool_uuid)
        payload: dict[str, Any] = {"verb": verb, "args": args or {}}
        if peer:
            payload["peer"] = peer
        headers = {"X-Awm-Plugin": PLUGIN, "X-Awm-Peer-Redirect": "1"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as cli:
                resp = await cli.post(f"{self.hub_url}/invoke",
                                      json={"name": name, "args": payload}, headers=headers)
        except httpx.HTTPError as exc:
            raise CallError(f"{name}.{verb}: gateway unreachable: {exc}") from exc
        if resp.status_code // 100 != 2:
            raise CallError(f"{name}.{verb}: HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        if isinstance(body, dict) and "peer_redirect" in body and peer:
            return await self._via_peer_edge(peer, name, payload)
        return _unwrap(body)

    async def _via_peer_edge(self, peer: str, name: str, payload: dict) -> Any:
        from awm import gatewayclient

        try:
            body = await gatewayclient.invoke_peer(
                peer, name, {"verb": payload["verb"], "args": payload["args"]},
                timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001 — a down peer is an expected answer here
            raise CallError(f"{name}.{payload['verb']} on {peer}: {exc}") from exc
        return _unwrap(body)

    async def up(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as cli:
                resp = await cli.get(f"{self.hub_url}/status")
            return resp.status_code // 100 == 2
        except httpx.HTTPError:
            return False
