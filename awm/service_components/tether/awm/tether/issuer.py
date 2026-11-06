"""The operator daemon's issuer: deneb's ``relay`` issues each machine slot through its door.

The daemon runs this once per request, as ``TETHER_ISSUER`` names it, with one
JSON object on stdin and one on stdout (the protocol is tether's
``crates/tether-operator/src/issuer.rs``). It calls ``relay issue`` through this
node's gateway on the ``channel`` service's account, read from the token file
the gateway minted for that service's current spawn. The bearer that gates the
tether relay never leaves deneb (``channels.md`` R147).
"""

from __future__ import annotations

import json
import sys
from typing import Any, Awaitable, Callable

#: How the daemon runs this module. The daemon outlives the plugin, so this
#: names the env's interpreter rather than anything the plugin holds.
ARGV = [sys.executable, "-m", "awm.tether.issuer"]

#: The door's own budget, less the daemon's margin.
TIMEOUT_S = 90.0

Call = Callable[[dict], Awaitable[Any]]


async def _through_gateway(request: dict) -> Any:
    from awm import gatewayclient

    return await gatewayclient.call("relay", "issue", request, timeout=TIMEOUT_S)


async def answer(request: Any, call: Call = _through_gateway) -> dict:
    """The relay's answer to one daemon request, or a ``ValueError`` that says why not."""
    if not isinstance(request, dict):
        raise ValueError("the request is a JSON object")
    try:
        result = await call(request)
    except Exception as exc:  # noqa: BLE001 — every failure is one line for the daemon
        detail = getattr(exc, "body", None) or str(exc)
        try:
            detail = json.loads(detail).get("detail") or detail
        except (ValueError, AttributeError):
            pass
        raise ValueError(f"relay issue: {str(detail).strip()}") from exc
    if not isinstance(result, dict):
        raise ValueError(f"relay issue answered {result!r}")
    return result


def main() -> int:
    import asyncio

    try:
        request = json.loads(sys.stdin.read() or "{}")
        result = asyncio.run(answer(request))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
