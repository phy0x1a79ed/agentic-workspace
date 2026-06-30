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


def test_frozen_mcp_names_present():
    names = {t.name for t in operations_to_mcp_tools(GATEWAY_OPERATIONS)}
    # Frozen, preserved names:
    assert {"awm_status", "awm_restart", "awm_mcp_sync"} <= names
    # Newly-exposed control ops:
    assert {"gateway_list", "gateway_deregister",
            "services_list", "services_start", "services_stop",
            "services_restart", "services_enable", "services_disable"} <= names


def test_services_op_schema_requires_name():
    tools = {t.name: t for t in operations_to_mcp_tools(GATEWAY_OPERATIONS)}
    schema = tools["services_stop"].inputSchema
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
    assert set(groups) == {"gateway", "services", "config"}
    gw = {c.name for c in groups["gateway"].registered_commands}
    sv = {c.name for c in groups["services"].registered_commands}
    cf = {c.name for c in groups["config"].registered_commands}
    # Generated CLI commands only (list/start stay hand-authored → not here):
    assert {"status", "restart", "mcp-sync", "list", "deregister"} <= gw
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
        return await catalog.dispatch("services_list", {})

    import anyio
    payload = json.loads(anyio.run(_go))
    assert "services" in payload
