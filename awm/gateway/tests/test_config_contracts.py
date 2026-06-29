"""The gateway config aggregator — one surface over every opt-in contract.

`config_contracts` folds each opted-in service's manifest `config` block + its
live RPC `config_get` values (degrading a down service to `unavailable`) plus
the gateway's own global slot; `config_set` routes a write back to the owning
service over RPC. These tests fake the registry + control RPC so no live hub is
touched.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.hub, pytest.mark.unit]

from awm.gateway import gateway_ops
from awm.gateway.gateway_ops import ConfigSetRequest, _op_config_contracts, _op_config_set
from awm.gateway.hub.registry import ServiceRecord


_AGENTS_CFG = {
    "title": "Agent default driver",
    "schema": {"type": "object", "properties": {"harness": {"type": "string"}}},
}


def _rec(name, *, kind="service", api=None, service_id=None):
    return ServiceRecord(
        name=name, prefix=f"/svc/{name}", kind=kind,
        api=api or {}, service_id=service_id or f"sid-{name}",
    )


class _FakeRegistry:
    def __init__(self, recs):
        self._recs = recs

    async def list(self):
        return list(self._recs)

    def get_by_name(self, kind, name):
        for r in self._recs:
            if r.kind == kind and r.name == name:
                return r
        return None


class _FakeChannel:
    def __init__(self, responder):
        self._responder = responder

    async def call(self, fn, args, as_=None):
        return self._responder(fn, args)


def _wire(monkeypatch, recs, controls):
    """Patch get_registry + rpc.get_control. ``controls`` maps service_id →
    a _FakeChannel (or None to simulate a registered-but-down service)."""
    from awm.gateway.hub import registry as reg_mod
    from awm.gateway.hub import rpc as rpc_mod

    monkeypatch.setattr(reg_mod, "get_registry", lambda: _FakeRegistry(recs))
    monkeypatch.setattr(rpc_mod, "get_control", lambda sid: controls.get(sid))


async def test_contracts_folds_values_and_skips_opt_outs(monkeypatch):
    recs = [
        _rec("agents", api={"config": _AGENTS_CFG}),
        _rec("2fa", api={"functions": [{"name": "ping"}]}),  # opted out
        _rec("dashboard", kind="page", api={"config": _AGENTS_CFG}),  # not a service
    ]
    controls = {
        "sid-agents": _FakeChannel(
            lambda fn, args: {
                "title": "Agent default driver",
                "schema": _AGENTS_CFG["schema"],
                "values": {"harness": "claude", "model": "haiku"},
            }
        ),
    }
    _wire(monkeypatch, recs, controls)

    out = (await _op_config_contracts())["contracts"]
    keys = [c["key"] for c in out]
    assert "agents" in keys
    assert "2fa" not in keys  # opt-out service is absent
    assert "dashboard" not in keys  # non-service kind is absent
    assert "gateway" in keys  # global slot always appended

    agents = next(c for c in out if c["key"] == "agents")
    assert agents["unavailable"] is False
    assert agents["values"] == {"harness": "claude", "model": "haiku"}
    assert agents["title"] == "Agent default driver"


async def test_down_service_degrades_to_unavailable(monkeypatch):
    recs = [_rec("agents", api={"config": _AGENTS_CFG})]
    _wire(monkeypatch, recs, {})  # no control channel → down

    agents = next(
        c for c in (await _op_config_contracts())["contracts"] if c["key"] == "agents"
    )
    assert agents["unavailable"] is True
    assert agents["values"] is None
    # The manifest schema still shows so the UI can render the (disabled) card.
    assert "harness" in agents["schema"]["properties"]


async def test_global_slot_present_and_empty(monkeypatch):
    _wire(monkeypatch, [], {})
    out = (await _op_config_contracts())["contracts"]
    glob = next(c for c in out if c["key"] == "gateway")
    assert glob["schema"]["properties"] == {}  # scaffolded, intentionally empty
    assert glob["values"] == {}


async def test_set_routes_to_owning_service(monkeypatch):
    seen = {}

    def _responder(fn, args):
        seen["fn"], seen["args"] = fn, args
        return {"title": "Agent default driver", "schema": _AGENTS_CFG["schema"],
                "values": args["values"]}

    recs = [_rec("agents", api={"config": _AGENTS_CFG})]
    _wire(monkeypatch, recs, {"sid-agents": _FakeChannel(_responder)})

    out = await _op_config_set(ConfigSetRequest(key="agents",
                                                values={"harness": "claude"}))
    assert seen["fn"] == "config_set"
    assert seen["args"] == {"values": {"harness": "claude"}}
    assert out["values"] == {"harness": "claude"}


async def test_set_unknown_key_raises(monkeypatch):
    _wire(monkeypatch, [], {})
    with pytest.raises(FileNotFoundError):
        await _op_config_set(ConfigSetRequest(key="nope", values={}))


async def test_set_service_without_contract_raises(monkeypatch):
    recs = [_rec("plain", api={"functions": []})]
    _wire(monkeypatch, recs, {})
    with pytest.raises(ValueError):
        await _op_config_set(ConfigSetRequest(key="plain", values={}))


async def test_set_down_service_raises(monkeypatch):
    recs = [_rec("agents", api={"config": _AGENTS_CFG})]
    _wire(monkeypatch, recs, {})  # registered but no channel
    with pytest.raises(RuntimeError):
        await _op_config_set(ConfigSetRequest(key="agents", values={}))


async def test_set_global_slot_is_noop(monkeypatch):
    _wire(monkeypatch, [], {})
    out = await _op_config_set(ConfigSetRequest(key="gateway", values={}))
    assert out["key"] == "gateway"
    assert out["values"] == {}
