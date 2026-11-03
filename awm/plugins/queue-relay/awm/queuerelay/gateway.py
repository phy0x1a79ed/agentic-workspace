"""Calls through the gateway (redux plugin contract, § Calling another plugin)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

from awm.queuerelay.relay import Refused, Unreachable

QUEUE_STORE_UUID = "d38d79bf-4752-445d-a17f-ff90ecbc1425"
APPROVER_UUID = "0db78f30-1121-416b-83e2-4fb162e134be"
REFLECTION_UUID = "550c10ed-c2cb-5175-acfa-b17e97b52d6c"


def resolved(uuid: str, fallback: str) -> dict[str, Any]:
    path = os.environ.get("AWM_RESOLVED_TOOLS")
    if path and Path(path).exists():
        rec = json.loads(Path(path).read_text()).get(uuid)
        if rec:
            return rec
    return {"name": fallback, "peer": None}


class GatewayTool:
    """One tool reached by UUID. Transport failures and 5xx become Unreachable."""

    def __init__(self, uuid: str, fallback: str, *, peer: str | None = None,
                 timeout: float = 15.0) -> None:
        self.uuid, self.fallback, self.peer, self.timeout = uuid, fallback, peer, timeout
        self.hub = os.environ.get("AWM_HUB_URL", "http://127.0.0.1:8765")

    async def call(self, verb: str, args: dict[str, Any], *, timeout: float | None = None) -> Any:
        rec = resolved(self.uuid, self.fallback)
        body: dict[str, Any] = {"name": rec["name"], "args": {"verb": verb, "args": args}}
        peer = self.peer or rec.get("peer")
        if peer:
            body["args"]["peer"] = peer
        try:
            async with httpx.AsyncClient(timeout=timeout or self.timeout, verify=False) as c:
                r = await c.post(f"{self.hub}/invoke", json=body,
                                 headers={"X-Awm-Plugin": "queue-relay"})
        except httpx.HTTPError as exc:
            raise Unreachable(str(exc)) from exc
        if r.status_code >= 500 or r.status_code in (404, 408, 429):
            raise Unreachable(f"{r.status_code} {r.text[:200]}")
        if r.status_code >= 400:
            raise Refused(r.json().get("detail", r.text) if r.content else str(r.status_code))
        res = r.json().get("result")
        return json.loads(res) if isinstance(res, str) else res
