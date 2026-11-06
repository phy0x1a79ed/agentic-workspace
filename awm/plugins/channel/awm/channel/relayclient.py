"""The host half of a room, on the public host's ``relay`` singleton, reached through its door.

This node's channel service calls it on its own account, naming the member
session in each call. The door stamps this node, and the relay takes the node's
word for the session (``fleet.md`` R98).
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from awm import gatewayclient

TOOL = "relay"

Call = Callable[[str, dict, float], Awaitable[Any]]


class RelayError(Exception):
    """The relay refused, or its door did not answer. The message says why."""


async def _through_gateway(verb: str, args: dict, timeout: float) -> Any:
    try:
        return await gatewayclient.call(TOOL, verb, args, timeout=timeout)
    except gatewayclient.GatewayCallError as exc:
        raise RelayError(_detail(exc.body)) from exc
    except Exception as exc:  # noqa: BLE001 — an unreachable gateway is one more refusal
        raise RelayError(f"the relay could not be reached: {exc}") from exc


def _detail(body: str) -> str:
    try:
        detail = json.loads(body).get("detail")
    except (ValueError, AttributeError):
        return body.strip()
    return str(detail if detail is not None else body).strip()


class RelayClient:
    def __init__(self, call: Call = _through_gateway) -> None:
        self._call = call

    @staticmethod
    def member(holder: dict) -> dict:
        return {"session": holder["session"], "name": holder.get("name") or "",
                "level": holder.get("level") or "untrusted"}

    async def open(self, holder: dict, persistent: bool) -> dict:
        return await self._call("open", {"member": self.member(holder),
                                         "persistent": persistent}, 60.0)

    async def join(self, holder: dict, name: str) -> dict:
        return await self._call("join", {"member": self.member(holder), "name": name}, 30.0)

    async def write(self, holder: dict, name: str, message: dict) -> dict:
        return await self._call("write", {"member": self.member(holder), "name": name,
                                          "message": message}, 60.0)

    async def close(self, holder: dict, name: str) -> dict:
        return await self._call("close", {"member": self.member(holder), "name": name}, 30.0)

    async def feed(self, live: list[str] | None, acks: list[dict], wait: float) -> dict:
        args: dict = {"acks": acks, "wait": wait}
        if live is not None:
            args["live"] = live
        return await self._call("feed", args, wait + 30.0)
