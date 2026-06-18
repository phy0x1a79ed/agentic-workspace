"""Feature-service tools mirror the live MCP surface onto the CLI.

The gateway control plane is generated from the static GATEWAY_OPERATIONS
registry (see test_gateway_ops.py). Feature-service tools instead come from a
live ``GET /tools`` snapshot and dispatch by name through ``POST /invoke`` —
the same surface the MCP proxy reads. These tests pin that generator:
grouping/exclusion, schema → option fidelity, and the ``/invoke`` dispatch +
render contract.
"""

from __future__ import annotations

import inspect

import pytest

pytestmark = [pytest.mark.hub, pytest.mark.unit]

import typer

from awm.gateway.operations import register_service_cli_commands


# A small fixed /tools snapshot covering the shapes that matter:
# required + optional + array params, a multi-word verb, an excluded
# control-plane op, and a name with no underscore (ungroupable).
_TOOLS = [
    {
        "name": "scope_create",
        "description": "Create a scope",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "project key"},
                "scope": {"type": "string", "description": "scope key"},
                "guests": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["project", "scope"],
        },
    },
    {
        "name": "skill_search",
        "description": "Search skills",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "scope_refresh",  # multi-word verb stays a single command
        "description": "Refresh scope indexes",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string"}, "scope": {"type": "string"}},
            "required": [],
        },
    },
    {  # excluded: owned by GATEWAY_OPERATIONS, wired statically elsewhere
        "name": "services_list",
        "description": "should be excluded",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {  # ungroupable (no underscore) — skipped
        "name": "ping",
        "description": "no domain",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

_EXCLUDE = {"services_list", "awm_status", "gateway_list"}


def _make_app():
    app = typer.Typer()
    # Pre-attach a `dev` group so the reserved-domain collision guard is exercised.
    app.add_typer(typer.Typer(), name="dev")
    groups = register_service_cli_commands(
        app, _TOOLS, api_func=lambda *a, **k: None, exclude_names=_EXCLUDE
    )
    return app, groups


def test_groups_and_commands_generated():
    _, groups = _make_app()
    assert set(groups) == {"scope", "skill"}
    scope_cmds = {c.name for c in groups["scope"].registered_commands}
    assert scope_cmds == {"create", "refresh"}
    assert {c.name for c in groups["skill"].registered_commands} == {"search"}


def test_excluded_and_ungroupable_names_skipped():
    _, groups = _make_app()
    # services_list is excluded → no `services` group from this generator.
    assert "services" not in groups
    # `ping` has no underscore → no group, no command.
    assert "ping" not in groups


def test_reserved_domain_not_clobbered():
    # A tool whose domain collides with an already-attached group (`dev`) is
    # skipped rather than registering a second `dev` group.
    tools = _TOOLS + [
        {"name": "dev_thing", "description": "x", "inputSchema": {"type": "object", "properties": {}}}
    ]
    app = typer.Typer()
    app.add_typer(typer.Typer(), name="dev")
    groups = register_service_cli_commands(
        app, tools, api_func=lambda *a, **k: None, exclude_names=set()
    )
    assert "dev" not in groups


def test_option_required_optional_and_array_fidelity():
    _, groups = _make_app()
    create = next(c for c in groups["scope"].registered_commands if c.name == "create")
    sig = inspect.signature(create.callback)
    params = sig.parameters
    # required project/scope have no default; optional array `guests` does.
    assert params["project"].default is inspect.Parameter.empty
    assert params["scope"].default is inspect.Parameter.empty
    assert params["guests"].default is not inspect.Parameter.empty


class _StubResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def test_dispatch_posts_name_and_args_to_invoke(capsys):
    calls = []

    def api(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return _StubResponse(200, {"result": '{"ok": true}'})

    app = typer.Typer()
    groups = register_service_cli_commands(
        app, _TOOLS, api_func=api, exclude_names=_EXCLUDE
    )
    create = next(c for c in groups["scope"].registered_commands if c.name == "create")

    # Invoke the generated handler directly (None options are dropped).
    create.callback(project="demo", scope="s1", guests=None)

    assert calls == [("POST", "/invoke", {"json": {"name": "scope_create", "args": {"project": "demo", "scope": "s1"}}})]
    # The `result` string is JSON → pretty-printed.
    out = capsys.readouterr().out
    assert '"ok": true' in out


def test_dispatch_error_exits_nonzero():
    def api(method, path, **kwargs):
        return _StubResponse(404, payload=None, text='{"detail": "Unknown tool"}')

    app = typer.Typer()
    groups = register_service_cli_commands(
        app, _TOOLS, api_func=api, exclude_names=_EXCLUDE
    )
    search = next(c for c in groups["skill"].registered_commands if c.name == "search")

    with pytest.raises(typer.Exit) as ei:
        search.callback(query="x")
    assert ei.value.exit_code == 1
