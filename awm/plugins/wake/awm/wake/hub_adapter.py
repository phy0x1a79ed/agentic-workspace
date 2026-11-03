"""The wake plugin's service: Wake-on-LAN and the standby pairing (fleet.md R139).

Run by ``run.sh`` as ``python -m awm.wake.hub_adapter``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter
from awm.wake import boot, config, magic, standby
from awm.wake.gateway import Gateway

log = logging.getLogger("awm.wake")

_P = {"type": "string", "required": True}

API_MANIFEST: dict[str, Any] = {
    "functions": [
        {"name": "wake", "effect": "write", "tool": "wake",
         "description": (
             "Start a home machine with Wake-on-LAN, then wait until it answers on ssh. "
             "Only the pairs in this node's wake table run (fleet.md R139); any other "
             "target is refused with the reason. Returns {target, "
             "status: already_up|sent|up|timeout, seconds?, sent_to?}."),
         "params": [{"name": "target", **_P, "description": "Node to wake, by its fleet name."},
                    {"name": "wait", "type": "boolean", "required": False,
                     "description": "Wait for ssh to answer. Default true."},
                    {"name": "budget_s", "type": "number", "required": False,
                     "description": "Seconds to wait. Default the target's budget, 180."}],
         "timeout": 400},
        {"name": "status", "effect": "read", "tool": "wake",
         "description": "Whether a wake target answers on ssh now. Returns {target, up}.",
         "params": [{"name": "target", **_P, "description": "Node to probe."}]},
        {"name": "nodes", "effect": "read", "tool": "wake",
         "description": ("This node's wake table: each target with its wakers, whether this "
                         "node may wake it, and the standby partner."),
         "params": []},
        {"name": "handover", "effect": "write", "tool": "wake",
         "description": (
             "Before this node restarts: ask the standby partner to hold a session that "
             "answers Tony, then reboot when `reboot` is true. Refuses, and leaves this node "
             "up, when the partner does not take the standby. Returns the standby session's "
             "job and Remote Control link; give Tony the link before the reboot."),
         "params": [{"name": "reason", **_P, "description": "Why this node restarts."},
                    {"name": "expect_down_s", "type": "number", "required": False,
                     "description": "Expected downtime in seconds. Default 120."},
                    {"name": "reboot", "type": "boolean", "required": False,
                     "description": "Reboot this node once the partner holds. Default false."}],
         "timeout": 120},
        {"name": "standby_take", "effect": "write", "tool": "wake",
         "description": ("Called by the partner's handover: hold a standby session for the "
                         "node that goes down. Idempotent while a hold is open."),
         "params": [{"name": "down", **_P, "description": "The node that goes down."},
                    {"name": "reason", **_P, "description": "Why it goes down."},
                    {"name": "expect_down_s", "type": "number", "required": False,
                     "description": "Expected downtime in seconds."}],
         "timeout": 120},
        {"name": "standby_release", "effect": "write", "tool": "wake",
         "description": ("Called by the returning node's boot runner: end the hold and pass "
                         "the revive report to the standby session."),
         "params": [{"name": "down", **_P, "description": "The node that came back."},
                    {"name": "report", "type": "object", "required": False,
                     "description": "The boot runner's report."}]},
        {"name": "standby_status", "effect": "read", "tool": "wake",
         "description": "The holds on this node and the partner's missed probes.",
         "params": []},
        {"name": "boot", "effect": "write", "tool": "wake",
         "description": ("Run the boot routine by hand: revive this node's pinned jobs "
                         "through the harness, then release the partner's hold. The boot "
                         "unit runs the same routine once per boot."),
         "params": [], "timeout": 300},
    ],
    "emitters": [],
    "sessions": [],
}

_store = standby.default_store()
_gw = Gateway()
_tasks: set[asyncio.Task] = set()


def _cfg() -> config.WakeConfig:
    return config.load()


async def _wake(args: dict) -> dict:
    cfg = _cfg()
    name = str(args["target"])
    refusal = cfg.refusal(name)
    if refusal:
        return {"ok": False, "reason": refusal}
    return await magic.wake(cfg.target(name), wait=args.get("wait", True) is not False,
                            budget_s=args.get("budget_s"))


async def _status(args: dict) -> dict:
    t = _cfg().target(str(args["target"]))
    return {"target": t.name, "up": await magic.answers(t.probe_host, t.probe_port)}


async def _nodes(args: dict) -> dict:
    cfg = _cfg()
    return {"node": cfg.node, "partner": cfg.standby.partner,
            "targets": [{"name": t.name, "wakers": list(t.wakers),
                         "may_wake": cfg.refusal(t.name) is None} for t in cfg.targets.values()]}


async def _refusable(coro) -> dict:
    try:
        return await coro
    except (standby.Refused, config.ConfigError) as exc:
        return {"ok": False, "reason": str(exc)}


async def _handover(args: dict) -> dict:
    return await _refusable(standby.handover(
        _cfg(), _gw, reason=str(args["reason"]),
        expect_down_s=float(args.get("expect_down_s") or 120),
        reboot=bool(args.get("reboot"))))


async def _take(args: dict) -> dict:
    return await _refusable(standby.take(
        _cfg(), _store, _gw, down=str(args["down"]), reason=str(args["reason"]),
        expect_down_s=args.get("expect_down_s")))


async def _release(args: dict) -> dict:
    return await standby.release(_cfg(), _store, _gw, down=str(args["down"]),
                                 report=args.get("report"))


async def _standby_status(args: dict) -> dict:
    return standby.status(_store)


async def _boot(args: dict) -> dict:
    return await boot.run(_cfg(), _gw, gateway_wait_s=30)


HANDLERS = {"wake": _wake, "status": _status, "nodes": _nodes, "handover": _handover,
            "standby_take": _take, "standby_release": _release,
            "standby_status": _standby_status, "boot": _boot}


async def _start_watchdog() -> None:
    try:
        _cfg()
    except config.ConfigError as exc:
        log.warning("no watchdog: %s", exc)
        return
    _tasks.add(asyncio.get_running_loop().create_task(
        standby.watchdog_loop(_cfg, _store, _gw)))


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    await ServiceAdapter("wake", API_MANIFEST, HANDLERS, on_start=_start_watchdog).run()


if __name__ == "__main__":
    asyncio.run(main())
