"""The boot runner: release the partner's hold once this node's pinned jobs are back.

A systemd user unit runs ``python -m awm.wake.boot`` once per boot, after the
gateway. The harness revives the pinned jobs by itself on its first start of a
boot. This runner waits for that revive's report (``revive_report``) and passes
it on with the release.
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
from awm.wake.gateway import WAKE, CallError, Gateway

log = logging.getLogger("awm.wake.boot")


async def run(cfg: wake_config.WakeConfig, gw: Gateway, *, gateway_wait_s: float = 600,
              revive_wait_s: float = 900, retry_s: float = 20,
              sleep: Callable[[float], Any] = asyncio.sleep,
              clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    report = {"node": cfg.node, **await _report(gw, gateway_wait_s, revive_wait_s, retry_s, sleep, clock)}
    return {**report, "release": await _release(cfg, gw, report)}


async def _report(gw: Gateway, gateway_wait_s: float, revive_wait_s: float, retry_s: float,
                  sleep: Callable[[float], Any], clock: Callable[[], float]) -> dict[str, Any]:
    """This boot's revive report from the harness, or why there is none."""
    t0 = clock()
    while not await gw.up():
        if clock() - t0 > gateway_wait_s:
            return {"error": "the gateway never answered"}
        await sleep(5)
    t0 = clock()
    error = "the harness never reported a revive for this boot"
    while clock() - t0 <= revive_wait_s:
        try:
            harness = await gw.harness()
            if harness is None:
                raise CallError("the gateway names no harness provider")
            out = await gw.call(harness, "revive_report", {})
            if out.get("this_boot"):
                return {"revive": out.get("report")}
        except CallError as exc:
            error = f"revive_report failed: {exc}"
            log.warning("%s", error)
        await sleep(retry_s)
    return {"error": error}


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
