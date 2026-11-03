from __future__ import annotations

import ast
from pathlib import Path

import pytest

from awm.wake import boot, config, standby
from tests.conftest import REFLECTION, TOML, WAKE, CallError, FakeGateway

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture()
def alcor_cfg():
    raw = {**TOML, "node": "alcor", "standby": {**TOML["standby"], "partner": "mizar"},
           "targets": {**TOML["targets"],
                       "mizar": {"mac": "00:23:24:bf:7e:4f", "broadcast": "172.16.0.255",
                                 "probe_host": "172.16.0.25", "wakers": ["alcor"]}}}
    return config.parse(raw)


@pytest.fixture()
def store(tmp_path):
    return standby.Store(tmp_path / "standby.json")


class Now:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


async def test_handover_takes_standby_on_partner_then_reboots(cfg):
    gw = FakeGateway({(WAKE, "standby_take"): {"ok": True, "job": "j1", "link": "https://l"}})
    ran = []
    out = await standby.handover(cfg, gw, reason="kernel update", reboot=True, runner=ran.append)
    assert gw.calls[0] == (WAKE, "standby_take",
                           {"down": "mizar", "reason": "kernel update", "expect_down_s": 120},
                           "alcor")
    assert out["rebooting"] and ran == [("true",)]
    assert "https://l" in out["tell_tony"]


async def test_handover_never_reboots_without_a_standby(cfg):
    gw = FakeGateway({(WAKE, "standby_take"): CallError("alcor unreachable")})
    ran = []
    with pytest.raises(standby.Refused, match="stays up"):
        await standby.handover(cfg, gw, reason="x", reboot=True, runner=ran.append)
    assert ran == []


async def test_take_starts_a_briefed_remote_control_session(alcor_cfg, store, gw):
    out = await standby.take(alcor_cfg, store, gw, down="mizar", reason="kernel update",
                             now=Now())
    assert out["ok"] and out["job"] == "abcd1234" and not out["reused"]
    _, verb, args, peer = gw.calls[0]
    assert verb == "start_other" and peer is None
    assert args["remote_control"] is True and args["name"] == "standby-for-mizar"
    assert "kernel update" in args["prompt"] and "reflection stop" in args["prompt"]


async def test_take_is_idempotent_while_holding(alcor_cfg, store, gw):
    await standby.take(alcor_cfg, store, gw, down="mizar", reason="a", now=Now())
    out = await standby.take(alcor_cfg, store, gw, down="mizar", reason="b", now=Now(1100))
    assert out["reused"] and gw.verbs().count("start_other") == 1


async def test_take_refuses_a_node_that_is_not_the_partner(alcor_cfg, store, gw):
    with pytest.raises(standby.Refused, match="standby only for mizar"):
        await standby.take(alcor_cfg, store, gw, down="capella", reason="x")


async def test_release_messages_the_session_with_the_report(alcor_cfg, store, gw):
    await standby.take(alcor_cfg, store, gw, down="mizar", reason="a", now=Now())
    out = await standby.release(alcor_cfg, store, gw, down="mizar",
                                report={"revive": {"revived": ["8162a704"]}})
    assert out["held"] and out["delivered"]
    _, verb, args, _ = gw.calls[-1]
    assert verb == "message" and args["job"] == "abcd1234" and "8162a704" in args["text"]
    assert standby.status(store)["holds"][0]["state"] == "released"


async def test_release_without_a_hold_is_a_quiet_yes(alcor_cfg, store, gw):
    assert await standby.release(alcor_cfg, store, gw, down="mizar") == {"ok": True, "held": False}


def probe_says(up):
    async def probe(h, p):
        return up
    return probe


async def test_watchdog_rewakes_past_deadline_with_backoff_and_cap(alcor_cfg, store, gw):
    now = Now()
    await standby.take(alcor_cfg, store, gw, down="mizar", reason="a", now=now)
    sent = []
    tick = lambda: standby.watchdog_tick(alcor_cfg, store, gw, probe=probe_says(False),  # noqa: E731
                                         sender=lambda t: sent.append(t.name) or ["b"], now=now)
    now.t += 200
    assert await tick() == []
    now.t += 101
    assert await tick() == ["rewake"]
    now.t += 30
    assert await tick() == []
    now.t += 120
    assert await tick() == ["rewake"]
    now.t += 500
    assert await tick() == []
    assert sent == ["mizar", "mizar"]


async def test_watchdog_starts_an_unplanned_hold_after_misses(alcor_cfg, store, gw):
    for _ in range(standby.UNPLANNED_AFTER - 1):
        assert await standby.watchdog_tick(alcor_cfg, store, gw, probe=probe_says(False)) == []
    assert await standby.watchdog_tick(alcor_cfg, store, gw,
                                       probe=probe_says(False)) == ["unplanned_hold"]
    hold = standby.status(store)["holds"][0]
    assert hold["planned"] is False and hold["down"] == "mizar"


async def test_watchdog_resets_misses_when_partner_answers(alcor_cfg, store, gw):
    await standby.watchdog_tick(alcor_cfg, store, gw, probe=probe_says(False))
    await standby.watchdog_tick(alcor_cfg, store, gw, probe=probe_says(True))
    assert standby.status(store)["misses"]["mizar"] == 0


async def test_watchdog_nags_once_when_up_but_never_released(alcor_cfg, store, gw):
    now = Now()
    await standby.take(alcor_cfg, store, gw, down="mizar", reason="a", now=now)
    run = lambda: standby.watchdog_tick(alcor_cfg, store, gw, probe=probe_says(True), now=now)  # noqa: E731
    assert await run() == []
    now.t += 301
    assert await run() == ["nagged"]
    now.t += 301
    assert await run() == []


async def test_boot_revives_then_releases_partner(cfg):
    revived = {"revived": ["c20d4fcd"], "live": [], "failed": []}
    gw = FakeGateway({(REFLECTION, "revive_pinned"): revived,
                      (WAKE, "standby_release"): {"ok": True, "held": True}})
    out = await boot.run(cfg, gw)
    assert gw.calls[0][:2] == (REFLECTION, "revive_pinned")
    assert gw.calls[1] == (WAKE, "standby_release",
                           {"down": "mizar", "report": {"node": "mizar", "revive": revived}},
                           "alcor")
    assert out["release"] == {"ok": True, "held": True}


async def test_boot_waits_for_gateway_and_releases_even_on_failure(cfg):
    gw = FakeGateway({(REFLECTION, "revive_pinned"): CallError("unknown verb")},
                     up=lambda n: True)
    slept = []

    async def sleep(s):
        slept.append(s)

    out = await boot.run(cfg, gw, revive_tries=2, sleep=sleep)
    assert "revive_pinned failed" in out["error"]
    assert gw.verbs() == ["revive_pinned", "revive_pinned", "standby_release"]


async def test_boot_gives_up_on_a_dead_gateway(cfg):
    clock = Now(0)

    async def sleep(s):
        clock.t += s

    gw = FakeGateway(up=False)
    out = await boot.run(cfg, gw, gateway_wait_s=20, sleep=sleep, clock=clock)
    assert out["error"] == "the gateway never answered"


def test_no_plugin_imports_another_plugins_code():
    src = Path(__file__).parents[1] / "awm" / "wake"
    for path in src.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = getattr(node, "module", None) or ""
                names = [mod] + [a.name for a in node.names]
                assert not any(n.startswith("awm.claudedaemon") or n.startswith("awm.cx")
                               or n.startswith("awm.reflection") for n in names), path


def test_manifest_matches_contract_v1():
    import tomllib
    import uuid

    m = tomllib.loads((Path(__file__).parents[1] / "awm-plugin.toml").read_text())
    assert m["plugin"]["name"] == "wake"
    assert m["tool"][0]["uuid"] == WAKE and m["tool"][0]["major"] == 1
    assert {"uuid": REFLECTION, "major": 1} in m["needs"]["tool"]
    assert REFLECTION == str(uuid.uuid5(uuid.NAMESPACE_URL, "awm:tool:reflection"))
    from awm.wake.hub_adapter import API_MANIFEST
    assert {f["tool"] for f in API_MANIFEST["functions"]} == {"wake"}
