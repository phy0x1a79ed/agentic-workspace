"""The gateway control plane is generated from GATEWAY_OPERATIONS.

These tests pin the three generated surfaces to one source: that the same
Operation registry yields the frozen MCP tool names, the kept HTTP routes, and
the CLI command set — plus direct coverage of the compiler hardening (async
handlers, DELETE, POST-with-path-and-no-body) the gateway ops rely on.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.hub, pytest.mark.unit]

from fastapi import FastAPI
from fastapi.testclient import TestClient

from awm.gateway.gateway_ops import GATEWAY_OPERATIONS
from awm.gateway.operations import (
    JsonOutput,
    Operation,
    Param,
    operations_to_mcp_tools,
    register_cli_commands,
    register_fastapi_routes,
)


# ---------------------------------------------------------------------------
# Compiler hardening — async handlers, DELETE, POST-path-no-body
# ---------------------------------------------------------------------------


def _mini_app(op: Operation) -> TestClient:
    app = FastAPI()
    register_fastapi_routes(app, [op])
    return TestClient(app, raise_server_exceptions=False)


def test_async_handler_awaited_over_http():
    """An async ``service_func`` is awaited by the generated route, not
    returned as a raw coroutine."""
    async def handler():
        return {"ran": "async"}

    op = Operation(
        name="t_async", description="", service_func=handler,
        http_method="GET", http_path="/t-async",
        cli_group="t", cli_command="async", output=JsonOutput(),
    )
    r = _mini_app(op).get("/t-async")
    assert r.status_code == 200
    assert r.json() == {"ran": "async"}


def test_post_with_path_param_no_body():
    """POST + a path param + no request body (the services/{name}/start
    shape) routes and extracts the path segment."""
    async def handler(name: str):
        return {"name": name, "started": True}

    op = Operation(
        name="t_start", description="", service_func=handler,
        http_method="POST", http_path="/t/{name}/start",
        cli_group="t", cli_command="start", output=JsonOutput(),
        params=[Param(name="name", type="string", required=True,
                      location="path", cli_type="argument")],
    )
    r = _mini_app(op).post("/t/widget/start")
    assert r.status_code == 200
    assert r.json() == {"name": "widget", "started": True}


def test_delete_with_optional_query_param():
    """DELETE through the generated handler: path param + optional query
    param, with the FileNotFoundError → 404 / FileExistsError → 409 mapping."""
    async def handler(name: str, kind: str | None = None):
        if name == "missing":
            raise FileNotFoundError("nope")
        if name == "ambiguous":
            raise FileExistsError("two kinds")
        return {"evicted": name, "kind": kind}

    op = Operation(
        name="t_dereg", description="", service_func=handler,
        http_method="DELETE", http_path="/t/{name}",
        cli_group="t", cli_command="deregister", output=JsonOutput(),
        params=[
            Param(name="name", type="string", required=True,
                  location="path", cli_type="argument"),
            Param(name="kind", type="string", required=False,
                  location="query", cli_type="option"),
        ],
    )
    client = _mini_app(op)
    assert client.delete("/t/x?kind=service").json() == {"evicted": "x", "kind": "service"}
    assert client.delete("/t/missing").status_code == 404
    assert client.delete("/t/ambiguous").status_code == 409


# ---------------------------------------------------------------------------
# GATEWAY_OPERATIONS → MCP surface (frozen names)
# ---------------------------------------------------------------------------


_VISIBLE = {
    "awm_status", "awm_registry_list", "awm_services_list", "awm_services_start",
    "awm_services_stop", "awm_services_restart", "awm_config_contracts",
    "operations_peer_list", "operations_peer_resolve",
}
_HIDDEN = {
    "awm_restart", "awm_mcp_sync", "awm_registry_deregister", "awm_services_reap",
    "awm_services_enable", "awm_services_disable", "awm_providers", "awm_config_set",
    "operations_peer_join", "operations_peer_add", "operations_peer_set",
    "operations_peer_grant", "operations_peer_revoke", "operations_peer_forget",
}


def test_the_native_verbs_published_to_mcp_are_exactly_the_visible_ones():
    names = {t.name for t in operations_to_mcp_tools(GATEWAY_OPERATIONS)}
    assert names == _VISIBLE


def test_every_native_op_is_a_verb_of_awm_or_operations():
    by_name = {op.name: op for op in GATEWAY_OPERATIONS}
    assert set(by_name) == _VISIBLE | _HIDDEN
    for name, op in by_name.items():
        assert op.mcp_tool in {"awm", "operations"}
        assert name == f"{op.mcp_tool}_{op.mcp_verb}"
        assert ("mcp" in op.surfaces) == (name in _VISIBLE)
        assert {"http"} <= op.surfaces, "a hidden verb still answers over HTTP"
    assert {op.name for op in GATEWAY_OPERATIONS if op.mcp_tool == "operations"} == {
        f"operations_peer_{v}" for v in
        ("join", "add", "set", "grant", "revoke", "list", "resolve", "forget")}


def test_services_op_schema_requires_name():
    tools = {t.name: t for t in operations_to_mcp_tools(GATEWAY_OPERATIONS)}
    schema = tools["awm_services_stop"].inputSchema
    assert schema["properties"]["name"]["type"] == "string"
    assert schema["required"] == ["name"]


def test_status_op_takes_no_args():
    tools = {t.name: t for t in operations_to_mcp_tools(GATEWAY_OPERATIONS)}
    assert tools["awm_status"].inputSchema == {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# GATEWAY_OPERATIONS → CLI surface
# ---------------------------------------------------------------------------


def test_cli_groups_and_commands_generated():
    import typer

    app = typer.Typer()
    groups = register_cli_commands(app, GATEWAY_OPERATIONS, api_func=lambda *a, **k: None)
    assert set(groups) == {"gateway", "services", "peer", "config"}, "the CLI groups keep their names"
    gw = {c.name for c in groups["gateway"].registered_commands}
    sv = {c.name for c in groups["services"].registered_commands}
    cf = {c.name for c in groups["config"].registered_commands}
    # Generated CLI commands only (list/start stay hand-authored → not here;
    # restart was hand-authored in cli.py, removed from generation):
    assert {"status", "mcp-sync", "list", "deregister"} <= gw
    assert {"stop", "restart", "enable", "disable"} <= sv
    # config_contracts is CLI-visible; config_set is mcp+http only (its values
    # dict doesn't round-trip cleanly through a CLI option).
    assert cf == {"contracts"}
    # list/start are mcp+http only — the generator must NOT emit them:
    assert "list" not in sv
    assert "start" not in sv


# ---------------------------------------------------------------------------
# GATEWAY_OPERATIONS → HTTP surface (against the real gateway app)
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(awm_workspace):
    from awm.gateway.server import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_status_route_generated_with_rich_payload(client):
    r = client.get("/status")
    assert r.status_code == 200
    data = r.json()
    # probe_existing_awm contract:
    assert data["status"] == "ok"
    assert isinstance(data["workspace_root"], str)
    assert isinstance(data["active_scopes"], int)
    # rich fields the generated route now also carries:
    assert "core_pid" in data and "core_python" in data


def test_hub_services_route_generated(client):
    r = client.get("/hub/services")
    assert r.status_code == 200
    assert r.json() == {"services": []}


def test_services_discovered_route_generated(client):
    r = client.get("/hub/services/discovered")
    assert r.status_code == 200
    assert "services" in r.json()


def test_deregister_unknown_is_404(client):
    r = client.delete("/hub/services/does-not-exist")
    assert r.status_code == 404


def test_services_start_unknown_is_404(client):
    r = client.post("/hub/services/does-not-exist/start")
    assert r.status_code == 404


def test_mcp_sync_route_generated(client):
    r = client.post("/mcp-sync")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Dispatch parity — the MCP/invoke path runs the SAME handler as HTTP
# ---------------------------------------------------------------------------


def test_dispatch_status_matches_http(client):
    import json

    from awm.gateway import catalog

    async def _go():
        return await catalog.dispatch("awm_status", {})

    import anyio
    raw = anyio.run(_go)
    payload = json.loads(raw)
    assert payload["status"] == "ok"
    # same shape as GET /status
    assert set(payload) == set(client.get("/status").json())


def test_dispatch_async_services_list(client):
    import json

    from awm.gateway import catalog

    async def _go():
        return await catalog.dispatch("awm_services_list", {})

    import anyio
    payload = json.loads(anyio.run(_go))
    assert "services" in payload


def test_a_hidden_native_verb_still_runs_by_its_flat_name_but_not_as_a_domain_verb():
    import anyio

    from awm.gateway import catalog

    async def flat():
        return await catalog.dispatch("awm_registry_deregister", {"name": "nope"})

    async def domain():
        return await catalog.dispatch("awm", {"verb": "registry_deregister",
                                              "args": {"name": "nope"}})

    with pytest.raises(FileNotFoundError):
        anyio.run(flat)
    with pytest.raises(ValueError, match="Unknown verb"):
        anyio.run(domain)


def test_the_old_native_names_still_reach_the_new_ones():
    import json

    import anyio

    from awm.gateway import catalog

    async def _go():
        return [await catalog.dispatch("services_list", {}),
                await catalog.dispatch("gateway", {"verb": "status"})]

    listing, status = anyio.run(_go)
    assert "services" in json.loads(listing) and json.loads(status)["status"] == "ok"


# ---------------------------------------------------------------------------
# awm status — one call for the node
# ---------------------------------------------------------------------------


class _Reading:
    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    async def __call__(self, service, fn):
        self.asked.append((service, fn))
        return self.answers.get(service)


async def test_status_adds_services_auth_node_role_and_compute(monkeypatch):
    from awm.gateway import gateway_ops as go

    async def listing():
        return {"services": [
            {"name": "scopes", "enabled": True, "running": True, "status": "ready", "pid": 1},
            {"name": "kb", "enabled": True, "running": False, "status": "stopped", "pid": None},
            {"name": "dev", "enabled": False, "running": False, "status": "stopped", "pid": None},
            {"name": "ssh", "enabled": True, "running": True, "status": "starting", "pid": 2}]}

    reading = _Reading({"auth": {"valid_generations": 2},
                        "compute": {"armed": "shadow"}})
    monkeypatch.setattr(go, "_op_services_list", listing)
    monkeypatch.setattr(go, "_service_reading", reading)
    monkeypatch.setenv("AWM_NODE_ROLE", "station")
    out = await go._op_status()
    assert out["status"] == "ok" and out["core_pid"] and "workspace_root" in out
    assert out["services"] == {"scopes": "ready", "kb": "stopped", "dev": "stopped",
                               "ssh": "starting"}
    assert out["services_down"] == ["kb", "ssh"], "a disabled service is not down"
    assert out["auth"] == {"valid_generations": 2}
    assert out["compute"] == {"armed": "shadow"}
    assert out["node_role"] == "station" and out["swarm"]
    assert sorted(reading.asked) == [("auth", "status"), ("compute", "status")]


async def test_status_survives_a_service_that_is_down_or_slow(monkeypatch):
    import asyncio

    from awm.gateway import gateway_ops as go
    from awm.gateway.hub import rpc
    from awm.gateway.hub.registry import ServiceRecord

    async def listing():
        return {"services": []}

    class Stub:
        def __init__(self, recs):
            self.recs = recs

        def get_by_name(self, kind, name):
            return self.recs.get(name)

    class Slow:
        async def call(self, fn, args, timeout=None):
            raise asyncio.TimeoutError

    class Broken:
        async def call(self, fn, args, timeout=None):
            raise RuntimeError("boom")

    recs = {"auth": ServiceRecord(name="auth", prefix="/svc/auth", kind="service",
                                  service_id="sid-auth"),
            "compute": ServiceRecord(name="compute", prefix="/svc/compute", kind="service",
                                     service_id="sid-compute")}
    channels = {"sid-auth": Slow(), "sid-compute": Broken()}
    monkeypatch.setattr(go, "_op_services_list", listing)
    monkeypatch.setattr("awm.gateway.hub.registry.get_registry", lambda: Stub(recs))
    monkeypatch.setattr(rpc, "get_control", lambda sid: channels.get(sid))
    out = await go._op_status()
    assert out["status"] == "ok"
    assert out["auth"] == {"error": "TimeoutError"}
    assert out["compute"] == {"error": "boom"}
    monkeypatch.setattr("awm.gateway.hub.registry.get_registry", lambda: Stub({}))
    assert (await go._op_status())["auth"] is None
