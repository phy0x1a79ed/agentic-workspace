"""Replay the routine plugin's real gateway calls against its manifest (plugin-contract v1.10).

The engine runs deploy batches through the real GatewayOutbound, on an httpx
transport that answers like the gateway. Every call must name a verb that the
manifest's `[[needs.tool]]` lists, or one of the plugin's own tools, and carry
the service token.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import httpx
import pytest

from awm.routine import fleetconf, outbound
from awm.routine.deploy import Deployer
from awm.routine.hub_adapter import DeployVerbs
from awm.routine.refresh import Refresher

pytestmark = pytest.mark.smoke

MANIFEST = Path(__file__).resolve().parents[1] / "awm-plugin.toml"
TOKEN = "svc-token-test"

INSTALL = '[fleet]\nhome = "mizar"\nnodes = ["mizar", "alcor"]\n'
FORWARD = '[fleet]\nhome = "mizar"\n[fleet.project.proj]\ncanonical = "alcor"\n'


def _needs() -> tuple[set[tuple[str, str]], set[str]]:
    m = tomllib.loads(MANIFEST.read_text())
    needed = {(t["name"], v) for t in m["needs"]["tool"] for v in t["verbs"]}
    return needed, {t["name"] for t in m["tool"]}


class Gateway:
    """Answers `POST /invoke` as the gateway would, and records each call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None, str | None]] = []
        self.fail_start = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if "verb" in body["args"]:  # a peer call: {name: tool, args: {verb, args, peer}}
            tool, inner = body["name"], body["args"]
        else:  # a local call: {name: "<tool>_<verb>", args}
            tool, verb = body["name"].split("_", 1)
            inner = {"verb": verb, "args": body["args"]}
        verb = inner["verb"]
        self.calls.append((tool, verb, inner.get("peer"), req.headers.get("X-Awm-Service-Token")))
        if (tool, verb) == ("reflection", "start_other") and self.fail_start:
            return httpx.Response(200, json={"result": {"error": "peer unreachable"}})
        result = {
            ("reflection", "start_other"): {"job": "j1"},
            ("reflection", "session_of"): {"known": True, "session_id": "cc01"},
            ("operations", "task_for_session"): {"task_id": "TASK0001"},
            ("routine", "refresh"): {"ok": True, "node": inner.get("peer"), "hooks": []},
            ("routine", "deploy"): {"ticket": "t1"},
        }.get((tool, verb), {"ok": True})
        return httpx.Response(200, json={"result": result})


@pytest.fixture()
def gateway(monkeypatch, tmp_path) -> Gateway:
    gw = Gateway()
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(gw), **kw))
    monkeypatch.setenv("AWM_HUB_URL", "http://gateway.test")
    token = tmp_path / "token"
    token.write_text(TOKEN)
    monkeypatch.setenv("AWM_HUB_TOKEN_FILE", str(token))
    return gw


def _deployer(store, ws, fleet: str, node: str = "mizar") -> Deployer:
    out = outbound.GatewayOutbound()
    ref = Refresher(out, node=node, checkout_root=lambda p: ws.proj / "release",
                    fleet_config=lambda: fleetconf.parse(fleet))
    return Deployer(store, out, workspace=ws.root, refresher=ref)


async def test_every_gateway_call_is_in_the_manifest(store, ws, gateway, monkeypatch):
    # A landed batch: card lookup, peer refresh, journal and card verdicts.
    wt = ws.scope("a", {"a.txt": "a\n"})
    monkeypatch.chdir(wt)
    verbs = DeployVerbs(_deployer(store, ws, INSTALL))
    await verbs.deploy({"_caller_pid": os.getpid()})
    await verbs.deployer.drain()
    # A failed batch whose fixer cannot start: start_other, then the operator DM.
    gateway.fail_start = True
    ws.scope("b", {"app.txt": "b\n"})
    ws.scope("c", {"app.txt": "c\n"})
    dep = _deployer(store, ws, INSTALL)
    for s in ("b", "c"):
        dep.submit(dep.resolve(project="proj", scope=s))
    await dep.drain()
    # A deploy forwarded to the canonical node.
    await DeployVerbs(_deployer(store, ws, FORWARD)).deploy({"project": "proj", "scope": "a"})

    needed, own = _needs()
    called = {(t, v) for t, v, _, _ in gateway.calls}
    assert {(t, v) for t, v in called if t not in own} <= needed
    assert needed <= called, f"manifest verbs no code path calls: {needed - called}"
    assert {("routine", "refresh"), ("routine", "deploy")} <= called
    assert all(tok == TOKEN for *_, tok in gateway.calls)
    assert ("routine", "refresh", "alcor") in {c[:3] for c in gateway.calls}
