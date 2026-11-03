"""The approver's calls out: queue-store and 2fa, by UUID through the gateway (plugin contract)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

QUEUE_STORE_UUID = "d38d79bf-4752-445d-a17f-ff90ecbc1425"
TWOFA_UUID = "892549fc-e262-5e5e-b420-c69134c77945"


def _resolved(uuid: str, fallback: str) -> dict[str, Any]:
    path = os.environ.get("AWM_RESOLVED_TOOLS")
    if path and Path(path).exists():
        rec = json.loads(Path(path).read_text()).get(uuid)
        if rec:
            return rec
    return {"name": fallback, "peer": None}


class GatewayTools:
    def __init__(self, hub: str | None = None, timeout: float = 20.0) -> None:
        self.hub = hub or os.environ.get("AWM_HUB_URL", "http://127.0.0.1:8765")
        self.timeout = timeout

    async def _call(self, uuid: str, fallback: str, verb: str, args: dict[str, Any]) -> Any:
        rec = _resolved(uuid, fallback)
        body: dict[str, Any] = {"name": rec["name"], "args": {"verb": verb, "args": args}}
        if rec.get("peer"):
            body["args"]["peer"] = rec["peer"]
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(f"{self.hub}/invoke", json=body, headers={"X-Awm-Plugin": "approver"})
        if r.status_code >= 400:
            raise RuntimeError(f"{fallback} {verb}: {r.status_code} {r.text[:300]}")
        res = r.json().get("result")
        return json.loads(res) if isinstance(res, str) else res

    async def store(self, verb: str, args: dict[str, Any]) -> Any:
        return await self._call(QUEUE_STORE_UUID, "queue-store", verb, args)

    async def twofa(self, verb: str, args: dict[str, Any]) -> Any:
        return await self._call(TWOFA_UUID, "2fa", verb, args)

    async def usage(self, page: str) -> None:
        """Record one page visit (plugin contract, § Pages on their own origin)."""
        try:
            async with httpx.AsyncClient(timeout=3) as c:
                await c.post(f"{self.hub}/usage", json={"surface": "page", "plugin": "approver",
                                                        "name": page})
        except httpx.HTTPError:
            pass
