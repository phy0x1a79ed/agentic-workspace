"""Operation registry — declarative definitions for MCP tools, FastAPI routes, and CLI commands."""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, Optional

import typer
from fastapi import HTTPException, Query
from mcp.types import Tool


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Param:
    """A single operation parameter."""

    name: str
    type: str  # "string", "integer", "array"
    required: bool = False
    default: Any = None
    description: str = ""
    location: str = "query"  # "query", "path"
    cli_type: str = "option"  # "option", "argument"
    cli_name: str | None = None  # Override for CLI flag, e.g. "--decision"


@dataclass
class Column:
    """A column in table output."""

    key: str
    header: str
    width: int = 20
    max_len: int | None = None


@dataclass
class JsonOutput:
    """Output as raw JSON."""

    pass


@dataclass
class TableOutput:
    """Output as formatted table."""

    list_key: str
    columns: list[Column]


@dataclass
class DetailOutput:
    """Output as detail header + body."""

    fields: list[tuple[str, str]]  # (label, dotted_key) pairs
    body_field: str = ""


_DEFAULT_SURFACES = frozenset({"cli", "mcp", "http"})


@dataclass
class Operation:
    """A declarative operation definition.

    ``surfaces`` controls which generated surfaces are emitted for this
    operation (subset of ``{"cli", "mcp", "http"}``). ``peer_only=True``
    short-circuits the surfaces to ``{"http"}`` and tags the route so
    callers / generators know it belongs to the peer-facing API (federation
    receivers — auth is per-peer bearer + ``X-Awm-From``, not operator
    bearer). ``tags`` is a free-form grouping hint (control-center vs
    internal, etc.) for future filtering / docs.
    """

    name: str
    description: str
    service_func: Callable
    http_method: str  # "GET", "POST"
    http_path: str  # "/sessions", "/sessions/{session_id}"
    cli_group: str
    cli_command: str
    output: JsonOutput | TableOutput | DetailOutput
    params: list[Param] = field(default_factory=list)
    request_model: type | None = None
    surfaces: frozenset[str] = field(default_factory=lambda: _DEFAULT_SURFACES)
    peer_only: bool = False
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.peer_only:
            # Peer-only operations are exclusively HTTP routes on the
            # peer-facing surface; never expose them via CLI/MCP.
            self.surfaces = frozenset({"http"})
        if not isinstance(self.surfaces, frozenset):
            self.surfaces = frozenset(self.surfaces)
        unknown = self.surfaces - {"cli", "mcp", "http"}
        if unknown:
            raise ValueError(
                f"Operation {self.name!r}: unknown surfaces {sorted(unknown)}"
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_path_params(path: str) -> set[str]:
    return set(re.findall(r"\{(\w+)\}", path))


def _py_type(p: Param):
    if p.type == "integer":
        return int
    if p.type == "boolean":
        return bool
    if p.type == "array":
        return list[str]
    return str


# ---------------------------------------------------------------------------
# Service dispatch
# ---------------------------------------------------------------------------


def _call_service(op: Operation, args: dict) -> Any:
    """Call the service function for an operation."""
    if op.request_model:
        req = op.request_model(**args)
        return op.service_func(req)
    param_names = {p.name for p in op.params}
    kwargs = {}
    for p in op.params:
        if p.name in args and args[p.name] is not None:
            kwargs[p.name] = args[p.name]
        elif p.default is not None:
            kwargs[p.name] = p.default
    return op.service_func(**kwargs)


def dispatch_operation(name: str, args: dict, operations: list[Operation]) -> Any | None:
    """Dispatch to the matching operation. Returns None if no match."""
    for op in operations:
        if op.name == name:
            return _call_service(op, args)
    return None


# ---------------------------------------------------------------------------
# MCP tool generation
# ---------------------------------------------------------------------------


def operations_to_mcp_tools(operations: list[Operation]) -> list[Tool]:
    return [_to_mcp_tool(op) for op in operations if "mcp" in op.surfaces]


def _to_mcp_tool(op: Operation) -> Tool:
    properties: dict[str, Any] = {}
    required: list[str] = []

    if op.request_model:
        schema = op.request_model.model_json_schema()
        properties = schema.get("properties", {})
        required = schema.get("required", [])
    else:
        for p in op.params:
            prop: dict[str, Any] = {"type": p.type}
            if p.description:
                prop["description"] = p.description
            if p.default is not None:
                prop["default"] = p.default
            if p.type == "array":
                prop["items"] = {"type": "string"}
            properties[p.name] = prop
            if p.required:
                required.append(p.name)

    input_schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        input_schema["required"] = required

    return Tool(name=op.name, description=op.description, inputSchema=input_schema)


# ---------------------------------------------------------------------------
# FastAPI route generation
# ---------------------------------------------------------------------------


def register_fastapi_routes(
    app, operations: list[Operation], *, include_peer_only: bool = False
) -> None:
    """Register HTTP routes for operations whose ``surfaces`` include ``"http"``.

    ``include_peer_only`` gates the peer-facing subset (``peer_only=True``);
    callers register user-facing and peer-facing routes on different apps
    (or under different mounts) to keep auth modes from mixing.
    """
    for op in operations:
        if "http" not in op.surfaces:
            continue
        if op.peer_only and not include_peer_only:
            continue
        if not op.peer_only and include_peer_only:
            continue
        handler = _make_fastapi_handler(op)
        app.add_api_route(
            op.http_path, handler, methods=[op.http_method], tags=list(op.tags),
        )


async def _await_if_needed(result: Any) -> Any:
    """Resolve a handler result that may be a coroutine. Lets a single
    ``service_func`` be sync (the native lifecycle ops) or async (the hub /
    services ops that drive the async registry) without the caller caring."""
    if inspect.isawaitable(result):
        return await result
    return result


def _make_fastapi_handler(op: Operation):
    """Build a FastAPI route handler for ``op``.

    Always ``async`` so an async ``service_func`` (the registry-touching hub /
    services handlers) can be awaited; a sync one returns immediately. Covers:

    * **POST/PUT/PATCH with a ``request_model``** — body-parsed.
    * **GET / DELETE / POST with path + query params and no body** — the
      ``services/{name}/start`` and ``deregister`` shapes; the signature is
      synthesized via ``exec`` so FastAPI's introspection extracts path vs
      query correctly, method-agnostically.

    Exception → HTTP: ``FileNotFoundError`` → 404, ``FileExistsError`` → 409,
    ``RuntimeError`` / ``ValueError`` → 400 (matching the catalog ``/invoke``
    translation, so a handler raises the same plain exceptions on every
    surface)."""
    if op.http_method in ("POST", "PUT", "PATCH") and op.request_model:
        svc = op.service_func

        async def body_handler(req):
            try:
                return await _await_if_needed(svc(req))
            except FileNotFoundError as e:
                raise HTTPException(404, str(e))
            except FileExistsError as e:
                raise HTTPException(409, str(e))
            except (RuntimeError, ValueError) as e:
                raise HTTPException(400, str(e))

        body_handler.__annotations__ = {"req": op.request_model}
        return body_handler

    # Path/query params, no body — build the signature dynamically so FastAPI
    # can inspect it. Works for GET, DELETE, and POST-without-a-body alike.
    path_params = _extract_path_params(op.http_path)
    sig_parts = []
    for p in op.params:
        pt = "int" if p.type == "integer" else "str"
        if p.name in path_params:
            sig_parts.append(f"{p.name}: {pt}")
        elif p.required:
            sig_parts.append(f"{p.name}: {pt} = Query(...)")
        else:
            opt = f"Optional[{pt}]"
            sig_parts.append(f"{p.name}: {opt} = Query({p.default!r})")

    param_dict = "{" + ", ".join(f"'{p.name}': {p.name}" for p in op.params) + "}"
    sig = ", ".join(sig_parts)

    code = (
        f"async def handler({sig}):\n"
        f"    try:\n"
        f"        return await _resolve(_call({param_dict}))\n"
        f"    except FileNotFoundError as e:\n"
        f"        raise HTTPException(404, str(e))\n"
        f"    except FileExistsError as e:\n"
        f"        raise HTTPException(409, str(e))\n"
        f"    except (RuntimeError, ValueError) as e:\n"
        f"        raise HTTPException(400, str(e))\n"
    )

    def call(kwargs):
        return _call_service(op, kwargs)

    ns: dict[str, Any] = {
        "Query": Query,
        "HTTPException": HTTPException,
        "Optional": Optional,
        "_call": call,
        "_resolve": _await_if_needed,
    }
    exec(code, ns)  # noqa: S102
    return ns["handler"]


# ---------------------------------------------------------------------------
# CLI command generation
# ---------------------------------------------------------------------------


def register_cli_commands(
    parent_app, operations: list[Operation], api_func: Callable
) -> dict[str, typer.Typer]:
    """Register operations as Typer CLI commands. Returns {group_name: Typer}.

    Only operations with ``"cli"`` in ``surfaces`` are emitted.
    """
    groups: dict[str, list[Operation]] = {}
    for op in operations:
        if "cli" not in op.surfaces:
            continue
        groups.setdefault(op.cli_group, []).append(op)

    result: dict[str, typer.Typer] = {}
    for group_name, ops in groups.items():
        group = typer.Typer(
            help=f"{group_name.title()} management", no_args_is_help=True
        )
        for op in ops:
            handler = _make_cli_handler(op, api_func)
            group.command(name=op.cli_command, help=op.description)(handler)
        parent_app.add_typer(group, name=group_name)
        result[group_name] = group

    return result


def _make_cli_handler(op: Operation, api_func: Callable) -> Callable:
    """Create a Typer-compatible handler using __signature__."""

    def handler(**kwargs):
        _cli_dispatch(op, api_func, kwargs)

    # Build a proper inspect.Signature for Typer introspection
    sig_params = []
    for p in op.params:
        base = _py_type(p)
        flag = p.cli_name or f"--{p.name.replace('_', '-')}"

        if p.cli_type == "argument":
            # A positional argument may be optional: ``required=False`` yields an
            # Optional annotation + a real default, which Typer renders as
            # ``[TOOL]``. Without this every argument was mandatory, so an op
            # whose positional is genuinely omittable (``awm peer providers``
            # with no tool = the whole fleet map) had to demote it to a flag.
            if p.required:
                ann = Annotated[base, typer.Argument(help=p.description)]
                default = inspect.Parameter.empty
            else:
                ann = Annotated[Optional[base],
                                typer.Argument(help=p.description)]
                default = p.default
        elif p.type == "array":
            ann = Annotated[Optional[list[str]], typer.Option(flag, help=p.description)]
            default = p.default
        elif p.required:
            ann = Annotated[base, typer.Option(flag, help=p.description)]
            default = inspect.Parameter.empty
        else:
            ann = Annotated[Optional[base], typer.Option(flag, help=p.description)]
            default = p.default

        sig_params.append(
            inspect.Parameter(
                p.name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=default,
                annotation=ann,
            )
        )

    handler.__signature__ = inspect.Signature(sig_params)  # type: ignore[attr-defined]
    handler.__doc__ = op.description
    return handler


def _cli_dispatch(op: Operation, api_func: Callable, kwargs: dict) -> None:
    """Execute a CLI command via the HTTP API and render output.

    Path params are substituted into the URL for every method (so
    ``services stop <name>`` → ``POST /hub/services/<name>/stop`` works); the
    rest go in the JSON body for body-methods and the query string otherwise."""
    path_params = _extract_path_params(op.http_path)
    url = op.http_path
    rest: dict[str, Any] = {}
    for k, v in kwargs.items():
        if v is None:
            continue
        if k in path_params:
            url = url.replace(f"{{{k}}}", str(v))
        else:
            rest[k] = v

    if op.http_method in ("POST", "PUT", "PATCH"):
        r = api_func(op.http_method, url, json=rest)
    else:
        # GET / DELETE — no body; non-path params ride the query string.
        r = api_func(op.http_method, url, params=rest)

    _render_output(op, r)


def _render_output(op: Operation, response) -> None:
    if response.status_code >= 400:
        typer.echo(f"Error ({response.status_code}): {response.text}", err=True)
        raise typer.Exit(1)

    data = response.json()

    if isinstance(op.output, JsonOutput):
        typer.echo(json.dumps(data, indent=2))

    elif isinstance(op.output, TableOutput):
        items = data.get(op.output.list_key, [])
        if not items:
            typer.echo(f"(no {op.cli_group} entries found)")
            return

        header = ""
        sep = ""
        for col in op.output.columns:
            header += f"{col.header:<{col.width}} "
            sep += f"{'-' * len(col.header):<{col.width}} "
        typer.echo(header.rstrip())
        typer.echo(sep.rstrip())

        for item in items:
            row = ""
            for col in op.output.columns:
                val = str(item.get(col.key, ""))
                if col.max_len and len(val) > col.max_len:
                    val = val[: col.max_len - 3] + "..."
                row += f"{val:<{col.width}} "
            typer.echo(row.rstrip())

        typer.echo(f"\nTotal: {data.get('total', len(items))} entry(ies)")

    elif isinstance(op.output, DetailOutput):
        for label, key in op.output.fields:
            val: Any = data
            for part in key.split("."):
                val = val.get(part, "n/a") if isinstance(val, dict) else "n/a"
            typer.echo(f"{label}: {val}")
        typer.echo("---")
        if op.output.body_field:
            typer.echo(str(data.get(op.output.body_field, "")))


# ---------------------------------------------------------------------------
# Service-tool CLI generation (live catalog → CLI, the MCP surface's twin)
# ---------------------------------------------------------------------------
# The gateway control plane above generates its CLI from the static
# GATEWAY_OPERATIONS registry. Feature-service tools, by contrast, have no
# per-function HTTP route — they are dispatched by name through ``POST /invoke``
# exactly like the MCP surface. This generator mirrors that surface onto the
# CLI: it consumes a live ``GET /tools`` snapshot (the same list MCP reads) and
# emits one ``awm <domain> <verb>`` command per tool, so the CLI tracks
# registered services with no static list to keep in sync.


def _json_schema_py_type(prop: dict):
    """Map a JSON-Schema ``type`` string (from an MCP ``inputSchema``) to a
    Python type for Typer introspection. Unknown / missing → ``str``."""
    t = prop.get("type")
    if t == "integer":
        return int
    if t == "number":
        return float
    if t == "boolean":
        return bool
    if t == "array":
        return list[str]
    return str


def _schema_param_to_signature(
    name: str, prop: dict, required: bool
) -> inspect.Parameter:
    """Build one ``inspect.Parameter`` from a JSON-Schema property entry.

    The CLI-surface analogue of ``_make_cli_handler``'s per-``Param`` loop, but
    sourced from an MCP ``inputSchema`` instead of an ``Operation.params`` list.
    Every field is an ``--flag`` option; arrays accept repeated values; a
    non-required field is ``Optional`` with the schema default (or ``None``)."""
    base = _json_schema_py_type(prop)
    flag = f"--{name.replace('_', '-')}"
    help_text = prop.get("description", "")

    if prop.get("type") == "array":
        ann = Annotated[Optional[list[str]], typer.Option(flag, help=help_text)]
        default = prop.get("default")
    elif required:
        ann = Annotated[base, typer.Option(flag, help=help_text)]
        default = inspect.Parameter.empty
    else:
        ann = Annotated[Optional[base], typer.Option(flag, help=help_text)]
        default = prop.get("default")

    return inspect.Parameter(
        name,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        default=default,
        annotation=ann,
    )


def _make_service_cli_handler(tool: dict, api_func: Callable) -> Callable:
    """Create a Typer handler for one service tool that dispatches via
    ``POST /invoke`` and renders the result."""
    tool_name = tool["name"]
    schema = tool.get("inputSchema") or {}
    props: dict = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])

    def handler(**kwargs):
        _invoke_dispatch(tool_name, api_func, kwargs)

    # A tool's inputSchema lists params in manifest/display order, which is free
    # to put a required field last (scope_post deliberately puts `body` last so a
    # serialization bleed has no trailing param to corrupt). But an
    # inspect.Signature forbids a no-default parameter after a defaulted one, so
    # build params then stable-sort required-before-optional. Every field is a
    # --flag Option and dispatch is by **kwargs, so reordering is invisible to
    # the CLI surface. See memory awm_cli_signature_param_order.
    sig_params = [
        _schema_param_to_signature(n, p, n in required) for n, p in props.items()
    ]
    sig_params.sort(key=lambda pm: pm.default is not inspect.Parameter.empty)
    handler.__signature__ = inspect.Signature(sig_params)  # type: ignore[attr-defined]
    handler.__doc__ = tool.get("description", "")
    return handler


def _invoke_dispatch(tool_name: str, api_func: Callable, kwargs: dict) -> None:
    """Dispatch a service tool through the by-name ``/invoke`` endpoint — the
    same path the MCP proxy uses — dropping unset (``None``) options."""
    args = {k: v for k, v in kwargs.items() if v is not None}
    # Generous ceiling (not the default 30s): a service verb may declare a longer
    # per-function timeout the catalog now honors (bulk re-embed / dedup). A fast
    # verb still returns immediately; this only bounds a hang.
    r = api_func("POST", "/invoke", json={"name": tool_name, "args": args}, timeout=600)
    _render_invoke_output(r)


def _render_invoke_output(response) -> None:
    """Render an ``/invoke`` response. Errors (status ≥ 400, incl. the
    structured ``{error_class, error}`` detail) go to stderr + exit 1; a success
    payload's ``result`` is pretty-printed (JSON if it parses, else raw)."""
    if response.status_code >= 400:
        typer.echo(f"Error ({response.status_code}): {response.text}", err=True)
        raise typer.Exit(1)

    data = response.json()
    result = data.get("result", data) if isinstance(data, dict) else data
    if isinstance(result, str):
        try:
            typer.echo(json.dumps(json.loads(result), indent=2))
        except (json.JSONDecodeError, ValueError):
            typer.echo(result)
    else:
        typer.echo(json.dumps(result, indent=2))


def register_service_cli_commands(
    parent_app,
    tools: list[dict],
    api_func: Callable,
    *,
    exclude_names: set[str],
) -> dict[str, typer.Typer]:
    """Generate ``awm <domain> <verb>`` CLI groups from a live ``GET /tools``
    snapshot, mirroring the MCP surface. Returns ``{group_name: Typer}``.

    ``tools`` is the MCP Tool list (each a dict with ``name``, ``description``,
    ``inputSchema``). A tool is grouped by its ``<domain>_<verb>`` name (split
    on the FIRST underscore: ``scope_create`` → group ``scope`` / command
    ``create``; remaining underscores in the verb become hyphens). Tools whose
    name is in ``exclude_names`` (the gateway control-plane ops, already wired
    from GATEWAY_OPERATIONS) are skipped, as are names with no underscore and
    any domain colliding with a group already attached to ``parent_app``."""
    reserved = {g.name for g in getattr(parent_app, "registered_groups", [])}

    groups: dict[str, list[dict]] = {}
    for tool in tools:
        name = tool.get("name", "")
        if not name or name in exclude_names or "_" not in name:
            continue
        domain = name.split("_", 1)[0]
        if domain in reserved:
            continue
        groups.setdefault(domain, []).append(tool)

    result: dict[str, typer.Typer] = {}
    for group_name, group_tools in sorted(groups.items()):
        group = typer.Typer(
            help=f"{group_name.title()} operations (mirrors the live MCP/catalog surface)",
            no_args_is_help=True,
        )
        for tool in group_tools:
            command = tool["name"].split("_", 1)[1].replace("_", "-")
            handler = _make_service_cli_handler(tool, api_func)
            group.command(name=command, help=tool.get("description", ""))(handler)
        parent_app.add_typer(group, name=group_name)
        result[group_name] = group

    return result
