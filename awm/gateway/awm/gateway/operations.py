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
            ann = Annotated[base, typer.Argument(help=p.description)]
            default = inspect.Parameter.empty
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
