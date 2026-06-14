"""The gateway's own control-plane operations, declared once.

This is the single source for every gateway control op that can be one. Each
:class:`~awm.gateway.operations.Operation` here compiles to all three surfaces
through the (now-wired) ``operations.py`` generators:

* **MCP** — :func:`operations_to_mcp_tools` projects the ``mcp``-surface ops
  into the ``/tools`` listing (merged with registered-service manifests in
  ``catalog.list_tools``); dispatch routes through ``catalog.dispatch``.
* **HTTP** — :func:`register_fastapi_routes` mounts the ``http``-surface ops on
  the FastAPI app at startup (``server.py``), alongside the few hand-authored
  routes that genuinely can't be declarative.
* **CLI** — :func:`register_cli_commands` emits the ``cli``-surface ops as Typer
  commands under ``awm gateway`` / ``awm services`` (``cli.py``); the
  hand-authored remainder (``init``/``serve``/``stop``/``refresh``/``register``)
  attaches to the same generated groups.

Adding a new gateway control op = adding an ``Operation`` here. Do **not**
hand-roll a parallel CLI command + HTTP route + native MCP tool — that is the
triplication this module exists to kill.

The handlers run **inside the gateway process** (they touch the process-global
hub registry / supervisor / lease manager), so the CLI surface reaches them
over HTTP via ``_api`` like every other command — never by importing them.
Several are ``async`` because the registry's mutate/snapshot methods are; the
compiler awaits coroutine handlers transparently (see
``operations._make_fastapi_handler`` / ``catalog.dispatch``).

Error contract: handlers raise plain ``FileNotFoundError`` / ``FileExistsError``
/ ``ValueError`` and the HTTP wrapper maps them to 404 / 409 / 400 (the lifted
hub handlers used to raise ``HTTPException`` directly — that doesn't survive the
generator, so they're re-shaped here).
"""

from __future__ import annotations

import os
import sys
from typing import Any

from awm.gateway.operations import JsonOutput, Operation, Param

_CLI = frozenset({"cli", "mcp", "http"})
_MCP_HTTP = frozenset({"mcp", "http"})


# ---------------------------------------------------------------------------
# Local handlers — run in-process on the gateway. Lazy imports inside each so
# this module stays import-cheap and free of catalog/registry import cycles.
# ---------------------------------------------------------------------------


def _op_status() -> dict[str, Any]:
    """Gateway-native status: workspace root, core process info, uptime.

    Identical payload on all three surfaces (CLI ``awm gateway status``, HTTP
    ``GET /status``, MCP ``awm_status``). ``active_scopes`` stays 0 until a
    scopes service registers — kept for shape stability and the
    ``_process_utils.probe_existing_awm`` health check."""
    from awm.config import WORKSPACE_ROOT
    from awm.gateway import catalog

    uptime = catalog.core_uptime()
    return {
        "status": "ok",
        "workspace_root": str(WORKSPACE_ROOT),
        "active_scopes": 0,
        "core_pid": os.getpid(),
        "core_uptime_s": uptime,
        "core_workspace_root": os.environ.get("AWM_WORKSPACE"),
        "core_python": sys.executable,
        "core_sys_path_head": sys.path[:3],
    }


def _op_restart() -> Any:
    """Restart the AWM core systemd unit (``awm.service``)."""
    from awm.gateway.core import restart_core

    return restart_core()


def _op_mcp_sync() -> Any:
    """Read workspace ``.mcp.json`` and regenerate the backend-specific MCP
    configs under ``.awm/``."""
    from awm.gateway.exports import sync_mcp_configs

    return sync_mcp_configs()


async def _op_gateway_list() -> dict[str, Any]:
    """List every hub registration (all kinds) + lease state."""
    from awm.gateway.hub.lease import get_lease_manager
    from awm.gateway.hub.registry import get_registry

    registry = get_registry()
    lm = get_lease_manager()
    out = []
    for rec in await registry.list():
        entry: dict[str, Any] = {
            "name": rec.name,
            "prefix": rec.prefix,
            "kind": rec.kind,
            "service_id": rec.service_id,
            "lease_held": lm.is_held(rec.service_id),
            "is_overlay": rec.is_overlay,
            "backend_status": rec.backend_status,
            "backend_pid": rec.backend_pid,
        }
        if rec.kind == "url":
            entry["url"] = rec.url
        elif rec.kind in ("static", "page"):
            entry["dir"] = rec.static_dir
        elif rec.kind == "service":
            entry["api"] = rec.api
        out.append(entry)
    return {"services": out}


async def _op_gateway_deregister(name: str, kind: str | None = None) -> dict[str, Any]:
    """Force-evict a registration by name (independent of its lease holder)."""
    from awm.gateway.hub.registry import PrefixConflict, get_registry

    registry = get_registry()
    try:
        rec = await registry.evict_by_name(name, kind=kind)
    except PrefixConflict as exc:
        # Ambiguous across kinds → 409 via the FileExistsError mapping.
        raise FileExistsError(str(exc)) from exc
    if rec is None:
        raise FileNotFoundError(f"unknown service: {name}")
    return {"evicted": {
        "name": rec.name,
        "prefix": rec.prefix,
        "kind": rec.kind,
        "service_id": rec.service_id,
    }}


async def _op_services_list() -> dict[str, Any]:
    """Discovery ⋈ enable-state ⋈ live registration — the ``awm services list``
    view (also the offline fallback's online counterpart)."""
    from awm.gateway.hub import discovery
    from awm.gateway.hub.registry import get_registry

    registry = get_registry()
    out = []
    for spec in discovery.discover_services():
        rec = registry.get_by_name("service", spec.name)
        out.append({
            "name": spec.name,
            "enabled": spec.enabled,
            "running": rec is not None,
            "status": rec.backend_status if rec is not None else "stopped",
            "pid": rec.backend_pid if rec is not None else None,
        })
    return {"services": out}


async def _op_services_start(name: str) -> dict[str, Any]:
    """Start a stopped service (idempotent — a running one is left alone)."""
    from awm.gateway.hub import discovery, supervisor
    from awm.gateway.hub.registry import get_registry

    registry = get_registry()
    if registry.get_by_name("service", name) is not None:
        return {"name": name, "started": False, "reason": "already-running"}
    spec = discovery.discover_service(name)
    if spec is None:
        raise FileNotFoundError(f"no service folder {name!r} with a run.sh")
    pid = supervisor.spawn_and_journal(name, list(spec.start_cmd), spec.cwd)
    return {"name": name, "started": True, "pid": pid}


async def _op_services_stop(name: str) -> dict[str, Any]:
    """Stop a running service (evict + kill pid group + drop journal entry)."""
    import asyncio

    from awm.gateway.hub import supervisor
    from awm.gateway.hub.registry import PrefixConflict, get_registry

    registry = get_registry()
    entry = supervisor.load_service_journal().get(name) or {}
    rec = registry.get_by_name("service", name)
    pid = (rec.backend_pid if rec is not None else None) or entry.get("last_pid")
    # Drop the journal entry FIRST, before evicting/killing, so the disconnect
    # watchdog sees a deliberate stop and does not respawn (see api/hub.py).
    supervisor.remove_service_journal_entry(name)
    if rec is not None:
        try:
            await registry.evict_by_name(name, kind="service")
        except PrefixConflict as exc:
            raise FileExistsError(str(exc)) from exc
    if pid:
        await asyncio.get_event_loop().run_in_executor(
            None, supervisor.kill_pid_group, pid)
    return {"name": name, "stopped": True, "killed_pid": pid}


async def _op_services_restart(name: str) -> dict[str, Any]:
    """Stop then start a service."""
    await _op_services_stop(name)
    return await _op_services_start(name)


async def _op_services_enable(name: str) -> dict[str, Any]:
    """Enable a service (persists across restart) and start it now."""
    from awm.gateway.hub import discovery

    discovery.set_enabled(name, True)
    return {"name": name, "enabled": True, "start": await _op_services_start(name)}


async def _op_services_disable(name: str) -> dict[str, Any]:
    """Disable a service (stays down across restart) and stop it now."""
    from awm.gateway.hub import discovery

    discovery.set_enabled(name, False)
    return {"name": name, "enabled": False, "stop": await _op_services_stop(name)}


# ---------------------------------------------------------------------------
# Operation registry — one entry per migratable control op.
# ---------------------------------------------------------------------------

_NAME_PARAM = Param(
    name="name", type="string", required=True,
    location="path", cli_type="argument",
    description="Service name (the folder name under awm/services/<name>/).",
)


GATEWAY_OPERATIONS: list[Operation] = [
    # --- gateway lifecycle / lease ----------------------------------------
    Operation(
        name="awm_status",
        description="Get AWM gateway status: workspace root, core process info, uptime.",
        service_func=_op_status,
        http_method="GET", http_path="/status",
        cli_group="gateway", cli_command="status",
        output=JsonOutput(), surfaces=_CLI,
    ),
    Operation(
        name="awm_restart",
        description="Restart the AWM core systemd unit (awm.service).",
        service_func=_op_restart,
        http_method="POST", http_path="/restart",
        cli_group="gateway", cli_command="restart",
        output=JsonOutput(), surfaces=_CLI,
    ),
    Operation(
        name="awm_mcp_sync",
        description="Read workspace .mcp.json and regenerate backend-specific MCP configs under .awm/.",
        service_func=_op_mcp_sync,
        http_method="POST", http_path="/mcp-sync",
        cli_group="gateway", cli_command="mcp-sync",
        output=JsonOutput(), surfaces=_CLI,
    ),
    Operation(
        name="gateway_list",
        description="List hub registrations (services / pages / url / static) and lease state.",
        service_func=_op_gateway_list,
        http_method="GET", http_path="/hub/services",
        cli_group="gateway", cli_command="list",
        output=JsonOutput(), surfaces=_CLI,
    ),
    Operation(
        name="gateway_deregister",
        description="Force-evict a hub registration by name (independent of its lease holder).",
        service_func=_op_gateway_deregister,
        http_method="DELETE", http_path="/hub/services/{name}",
        cli_group="gateway", cli_command="deregister",
        output=JsonOutput(),
        params=[
            Param(name="name", type="string", required=True,
                  location="path", cli_type="argument",
                  description="Registration name to evict."),
            Param(name="kind", type="string", required=False,
                  location="query", cli_type="option",
                  description="Disambiguate if the name exists across kinds "
                              "(page|service|url|static)."),
        ],
        surfaces=_CLI,
    ),
    # --- feature-service lifecycle (awm services *) -----------------------
    # `list` and `start` keep a hand-authored CLI command (offline fallback /
    # --all) but their HTTP route + MCP tool are generated from here, so the
    # handler is still single-sourced. Hence surfaces={mcp,http}.
    Operation(
        name="services_list",
        description="List discovered services with their enable + running state.",
        service_func=_op_services_list,
        http_method="GET", http_path="/hub/services/discovered",
        cli_group="services", cli_command="list",
        output=JsonOutput(), surfaces=_MCP_HTTP,
    ),
    Operation(
        name="services_start",
        description="Start a stopped service (idempotent).",
        service_func=_op_services_start,
        http_method="POST", http_path="/hub/services/{name}/start",
        cli_group="services", cli_command="start",
        output=JsonOutput(), params=[_NAME_PARAM], surfaces=_MCP_HTTP,
    ),
    Operation(
        name="services_stop",
        description="Stop a running service (evict + kill + drop journal entry).",
        service_func=_op_services_stop,
        http_method="POST", http_path="/hub/services/{name}/stop",
        cli_group="services", cli_command="stop",
        output=JsonOutput(), params=[_NAME_PARAM], surfaces=_CLI,
    ),
    Operation(
        name="services_restart",
        description="Restart a service (stop then start).",
        service_func=_op_services_restart,
        http_method="POST", http_path="/hub/services/{name}/restart",
        cli_group="services", cli_command="restart",
        output=JsonOutput(), params=[_NAME_PARAM], surfaces=_CLI,
    ),
    Operation(
        name="services_enable",
        description="Enable a service (persists across restart) and start it now.",
        service_func=_op_services_enable,
        http_method="POST", http_path="/hub/services/{name}/enable",
        cli_group="services", cli_command="enable",
        output=JsonOutput(), params=[_NAME_PARAM], surfaces=_CLI,
    ),
    Operation(
        name="services_disable",
        description="Disable a service (stays down across restart) and stop it now.",
        service_func=_op_services_disable,
        http_method="POST", http_path="/hub/services/{name}/disable",
        cli_group="services", cli_command="disable",
        output=JsonOutput(), params=[_NAME_PARAM], surfaces=_CLI,
    ),
]
