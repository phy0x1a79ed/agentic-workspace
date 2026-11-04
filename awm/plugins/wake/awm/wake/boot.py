"""The boot runner: revive this node's pinned jobs, then release the partner's hold.

A systemd user unit runs ``python -m awm.wake.boot`` once per boot, after the
gateway. The revival itself belongs to the harness (``revive_pinned``). This
runner only waits for the gateway, calls it, and passes the report on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from collections.abc import Callable
from typing import Any

from awm.wake import config as wake_config
from awm.wake.gateway import REFLECTION, WAKE, CallError, Gateway

log = logging.getLogger("awm.wake.boot")


async def run(cfg: wake_config.WakeConfig, gw: Gateway, *, gateway_wait_s: float = 600,
              revive_tries: int = 5, retry_s: float = 20,
              sleep: Callable[[float], Any] = asyncio.sleep,
              clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    t0 = clock()
    while not await gw.up():
        if clock() - t0 > gateway_wait_s:
            report: dict[str, Any] = {"node": cfg.node, "error": "the gateway never answered"}
            return {**report, "release": await _release(cfg, gw, report)}
        await sleep(5)

    # The redux gateway serves revive_pinned on the harness provider. Today's
    # gateway serves it as a hidden reflection verb.
    harness = await gw.harness() or REFLECTION
    revived: Any = None
    error = None
    for attempt in range(revive_tries):
        try:
            revived = await gw.call(harness, "revive_pinned", {})
            error = None
            break
        except CallError as exc:
            error = str(exc)
            log.warning("revive_pinned attempt %d failed: %s", attempt + 1, exc)
            await sleep(retry_s)
    report = {"node": cfg.node, "revive": revived} if error is None else {
        "node": cfg.node, "error": f"revive_pinned failed: {error}"}
    return {**report, "release": await _release(cfg, gw, report)}


async def _release(cfg: wake_config.WakeConfig, gw: Gateway, report: dict[str, Any]) -> Any:
    partner = cfg.standby.partner
    if not partner:
        return None
    try:
        return await gw.call(WAKE, "standby_release", {"down": cfg.node, "report": report},
                             peer=partner)
    except CallError as exc:
        return {"ok": False, "error": str(exc)}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        cfg = wake_config.load()
    except wake_config.ConfigError as exc:
        log.error("%s", exc)
        return 2
    result = asyncio.run(run(cfg, Gateway()))
    print(json.dumps(result, sort_keys=True))
    return 0 if "error" not in result else 1


if __name__ == "__main__":
    sys.exit(main())
