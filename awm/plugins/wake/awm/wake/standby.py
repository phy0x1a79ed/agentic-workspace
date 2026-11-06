"""The standby pairing of fleet.md R139.

One node of the pair restarts. Its partner holds a standby session that answers
Tony, watches the node, wakes it when it stays down past its deadline, and stops
once the node's boot runner releases it. A node that stops answering without a
handover gets the same standby, so a crash or a power cut is covered too.

The record of each hold lives in ``standby.json`` on the holding node. Every
function here takes its clock, gateway and probes as arguments, so the tests
drive it with fakes.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from awm.wake import magic
from awm.wake.config import WakeConfig
from awm.wake.gateway import REFLECTION, WAKE, CallError, Gateway

#: Consecutive failed probes of the partner, one per watchdog tick, before an
#: unplanned hold starts.
UNPLANNED_AFTER = 4


class Refused(Exception):
    pass


class Store:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {"holds": {}, "misses": {}}
        data.setdefault("holds", {})
        data.setdefault("misses", {})
        return data

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, self.path)


def default_store() -> Store:
    awm_dir = os.environ.get("AWM_DIR") or str(Path.home() / "agentic_workspace" / ".awm")
    return Store(Path(awm_dir) / "wake" / "standby.json")


def brief(cfg: WakeConfig, down: str, reason: str, deadline_at: float, planned: bool) -> str:
    when = time.strftime("%H:%M UTC", time.gmtime(deadline_at))
    cause = (f"{down} is restarting on purpose: {reason}." if planned else
             f"{down} stopped answering with no handover: {reason}.")
    return (
        f"You are the standby on {cfg.node} for {down} (fleet.md R139). {cause}\n"
        f"1. Tony reaches you through this session's Remote Control link. Answer him "
        f"from {cfg.node}. Nothing on {down} is reachable while it is down: its sessions, "
        f"its tethers and its ssh routes wait for it.\n"
        f"2. The wake plugin on {cfg.node} watches {down}. It sends Wake-on-LAN if {down} "
        f"is still down at {when}, and tells you each time. `wake standby_status` shows the hold.\n"
        f"3. When {down} boots, its boot runner revives its pinned jobs and releases this hold. "
        f"The release arrives here as a message with the revive report.\n"
        f"4. On the release, tell Tony what came back and what failed, in one short message. "
        f"Then stop this session with reflection stop.\n"
        f"5. If {down} stays down after the wakes, tell Tony and keep answering him. "
        f"Do not try to repair {down} from here."
    )


async def take(cfg: WakeConfig, store: Store, gw: Gateway, *, down: str, reason: str,
               expect_down_s: float | None = None, planned: bool = True,
               now: Callable[[], float] = time.time) -> dict[str, Any]:
    """Hold the standby for ``down``. Only the configured partner may hand over."""
    if down != cfg.standby.partner:
        raise Refused(f"{cfg.node} is standby only for {cfg.standby.partner or 'nobody'}, not {down}")
    data = store.load()
    held = data["holds"].get(down)
    t = now()
    deadline_at = t + max(expect_down_s or 0, cfg.standby.deadline_s)
    if held and held["state"] == "holding":
        held["deadline_at"] = max(held["deadline_at"], deadline_at)
        store.save(data)
        return {"ok": True, "reused": True, **_public(held)}
    started = await gw.call(REFLECTION, "start_other", {
        "project": cfg.standby.session_project, "scope": cfg.standby.session_scope,
        "name": f"standby-for-{down}", "remote_control": True, "brief": False,
        "started_by": f"the wake plugin on {cfg.node}",
        "model": "sonnet", "effort": "medium",
        "prompt": brief(cfg, down, reason, deadline_at, planned)})
    if not isinstance(started, dict) or not started.get("job"):
        raise Refused(f"no standby session started on {cfg.node}: {started!r}")
    held = {"down": down, "reason": reason, "planned": planned, "state": "holding",
            "taken_at": t, "deadline_at": deadline_at, "job": started["job"],
            "link": started.get("link"), "rewakes": 0, "last_rewake_at": None,
            "up_seen_at": None, "nagged": False}
    data["holds"][down] = held
    data["misses"].pop(down, None)
    store.save(data)
    return {"ok": True, "reused": False, **_public(held)}


async def release(cfg: WakeConfig, store: Store, gw: Gateway, *, down: str,
                  report: dict[str, Any] | None = None,
                  now: Callable[[], float] = time.time) -> dict[str, Any]:
    data = store.load()
    held = data["holds"].get(down)
    if not held or held["state"] != "holding":
        return {"ok": True, "held": False}
    held.update(state="released", released_at=now(), report=report or {})
    store.save(data)
    note = (f"Release from {down}: it is back. Revive report: "
            f"{json.dumps(report or {}, sort_keys=True)}. Tell Tony, then stop this session.")
    delivered = await _tell(gw, held["job"], note)
    return {"ok": True, "held": True, "job": held["job"], "delivered": delivered}


def status(store: Store) -> dict[str, Any]:
    data = store.load()
    return {"holds": [_public(h) for h in data["holds"].values()],
            "misses": data["misses"]}


async def watchdog_tick(cfg: WakeConfig, store: Store, gw: Gateway, *,
                        probe: Callable[[str, int], Any] = magic.answers,
                        sender: Callable[..., list[str]] = magic.send,
                        now: Callable[[], float] = time.time) -> list[str]:
    """One pass over the partner: start an unplanned hold, re-wake, or nag."""
    partner = cfg.standby.partner
    if not partner:
        return []
    target = cfg.target(partner)
    alive = await probe(target.probe_host, target.probe_port)
    data = store.load()
    held = data["holds"].get(partner)
    events: list[str] = []
    t = now()

    if not held or held["state"] != "holding":
        misses = 0 if alive else data["misses"].get(partner, 0) + 1
        data["misses"][partner] = misses
        store.save(data)
        if misses >= UNPLANNED_AFTER:
            try:
                await take(cfg, store, gw, down=partner, planned=False, now=now,
                           reason=f"no answer on ssh for {misses} watchdog ticks")
                events.append("unplanned_hold")
            except (Refused, CallError) as exc:
                events.append(f"unplanned_hold_failed: {exc}")
        return events

    if alive:
        if held["up_seen_at"] is None:
            held["up_seen_at"] = t
        elif not held["nagged"] and t - held["up_seen_at"] > cfg.standby.deadline_s:
            held["nagged"] = True
            await _tell(gw, held["job"], f"{partner} answers ssh but has not released the hold "
                        f"after {cfg.standby.deadline_s:.0f}s. Its boot runner may have failed: "
                        f"check its pinned jobs and tell Tony.")
            events.append("nagged")
        store.save(data)
        return events

    held["up_seen_at"] = None
    due = t >= held["deadline_at"] and held["rewakes"] < cfg.standby.rewake_max and (
        held["last_rewake_at"] is None or t - held["last_rewake_at"] >= cfg.standby.rewake_every_s)
    if due and cfg.refusal(partner) is None:
        sent = sender(target)
        held["rewakes"] += 1
        held["last_rewake_at"] = t
        store.save(data)
        await _tell(gw, held["job"], f"{partner} is still down past its deadline. Sent "
                    f"Wake-on-LAN {held['rewakes']} of {cfg.standby.rewake_max} to {', '.join(sent)}.")
        events.append("rewake")
    return events


async def handover(cfg: WakeConfig, gw: Gateway, *, reason: str, expect_down_s: float = 120,
                   reboot: bool = False,
                   runner: Callable[[tuple[str, ...]], Any] | None = None) -> dict[str, Any]:
    """Hand Tony to the partner, then reboot this node when ``reboot`` is set."""
    partner = cfg.standby.partner
    if not partner:
        raise Refused(f"{cfg.node} has no standby partner in wake.toml")
    try:
        held = await gw.call(WAKE, "standby_take", {
            "down": cfg.node, "reason": reason, "expect_down_s": expect_down_s}, peer=partner)
    except CallError as exc:
        raise Refused(f"{partner} did not take the standby, so {cfg.node} stays up: {exc}") from exc
    if not isinstance(held, dict) or not held.get("ok"):
        raise Refused(f"{partner} did not take the standby, so {cfg.node} stays up: {held!r}")
    out = {"ok": True, "partner": partner, "standby_job": held.get("job"),
           "standby_link": held.get("link"), "rebooting": False,
           "tell_tony": f"{partner} holds the standby while {cfg.node} restarts"
                        + (f": {held['link']}" if held.get("link") else ".")}
    if reboot:
        (runner or _spawn)(cfg.standby.reboot_command)
        out["rebooting"] = True
    return out


def _spawn(argv: tuple[str, ...]) -> None:
    subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


async def _tell(gw: Gateway, job: str, text: str) -> bool:
    try:
        await gw.call(REFLECTION, "message", {"job": job, "text": text})
        return True
    except CallError:
        return False


def _public(held: dict[str, Any]) -> dict[str, Any]:
    return {k: held.get(k) for k in ("down", "state", "planned", "reason", "job", "link",
                                     "taken_at", "deadline_at", "rewakes")}


async def watchdog_loop(cfg_loader: Callable[[], WakeConfig], store: Store, gw: Gateway,
                        every_s: float = 30.0) -> None:
    while True:
        try:
            await watchdog_tick(cfg_loader(), store, gw)
        except Exception:  # noqa: BLE001 — one bad tick must not end the watch
            import logging
            logging.getLogger("awm.wake").exception("watchdog tick failed")
        await asyncio.sleep(every_s)
