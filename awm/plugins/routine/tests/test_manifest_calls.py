"""Replay the routine plugin's real gateway calls against its manifest (plugin-contract § Calls).

A failed routine runs its failure chain through the real GatewayOutbound, on an
httpx transport that answers like the gateway. Each tool the manifest's
`[[needs.tool]]` names is one the chain calls, and every call carries the service token.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import httpx
import pytest

from awm.routine import outbound
from awm.routine.runner import Runner

from tests.conftest import write_routine

pytestmark = pytest.mark.smoke

MANIFEST = Path(__file__).resolve().parents[1] / "awm-plugin.toml"
TOKEN = "svc-token-test"

FAILING = """[routine]
name = "nightly"
description = "fails"
[[step]]
name = "boom"
run = "exit 3"
[fixer]
peer = "alcor"
"""


def _needs() -> set[str]:
    m = tomllib.loads(MANIFEST.read_text())
    return {t["name"] for t in m["needs"]["tool"]}


class Gateway:
    """Answers `POST /invoke` as the gateway would, and records each call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None, str | None]] = []
        self.fail_start = False
        self.sessions: list[dict] = []

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
            ("reflection", "list_other"): {"ok": True, "sessions": self.sessions},
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


async def test_each_needed_tool_is_one_the_failure_chain_calls(store, gateway, project_repo):
    runner = Runner(store, outbound.GatewayOutbound())
    runner.install("proj", str(write_routine(project_repo, "nightly", FAILING)))
    # A failed run starts its fixer. A fixer that cannot start falls back to the DM.
    await runner.trigger("proj", "nightly", wait=True)
    gateway.fail_start = True
    await runner.trigger("proj", "nightly", wait=True)

    called = {(t, v) for t, v, _, _ in gateway.calls}
    assert _needs() <= {t for t, _ in called}
    assert ("reflection", "start_other") in called
    assert all(tok == TOKEN for *_, tok in gateway.calls)
