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
import re
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

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


def _record_is_live(rec: Any) -> bool:
    """Is this registry record backed by something that actually exists?

    A record alone is NOT evidence. ``stop`` can leave a stub behind — no pid,
    lease released — and a guard that trusts the dictionary then refuses to
    start the service forever, which is what turned a two-command recovery
    into a manual fight on 2026-07-26. Mirrors the duplicate-instance guard in
    ``api/hub.py``: a held control-WS lease, or a pid that is genuinely alive.
    """
    from awm.gateway.hub import supervisor
    from awm.gateway.hub.lease import get_lease_manager

    try:
        if get_lease_manager().is_held(rec.service_id):
            return True
    except Exception:  # noqa: BLE001 — an unanswerable lease is not a yes
        pass
    return supervisor.pid_alive(rec.backend_pid)


async def _op_services_start(name: str) -> dict[str, Any]:
    """Start a stopped service (idempotent — a running one is left alone)."""
    import logging

    from awm.gateway.hub import discovery, supervisor
    from awm.gateway.hub.registry import get_registry

    log = logging.getLogger("awm.gateway.ops")
    registry = get_registry()
    existing = registry.get_by_name("service", name)
    if existing is not None and _record_is_live(existing):
        return {"name": name, "started": False, "reason": "already-running"}
    if existing is not None:
        log.warning("services start %s: stale registry record (pid=%r, lease "
                    "not held) — evicting it and starting fresh",
                    name, existing.backend_pid)
        try:
            await registry.evict_by_name(name, kind="service")
        except Exception as exc:  # noqa: BLE001 — the start is what matters
            log.warning("services start %s: could not evict the stale record: "
                        "%s", name, exc)
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


# --- reap: kill orphaned hub_adapter processes -----------------------------

class ReapRequest(BaseModel):
    """Body for ``services reap`` — ``dry_run`` lists without killing."""

    dry_run: bool = False


# Matches the ``awm.<svc>.hub_adapter`` module token in a process cmdline (the
# only thing a feature service is ever launched as), e.g.
# ``python -m awm.agents.hub_adapter``. The gateway itself is ``awm.gateway
# serve`` and the MCP proxy is ``awm-mcp`` — neither matches.
_HUB_ADAPTER_RE = re.compile(r"awm\.[\w.]*hub_adapter")


def _origin_of(url: str | None) -> str:
    """Reduce a hub URL to ``host:port`` for matching — ignoring scheme, path,
    and trailing slash, the things that differ between an injected
    ``AWM_HUB_URL`` and ``default_hub_url()``. Empty string if unparseable."""
    if not url:
        return ""
    from urllib.parse import urlparse

    parsed = urlparse(url if "//" in url else "//" + url, scheme="http")
    host = parsed.hostname or ""
    return f"{host}:{parsed.port}" if parsed.port else host


def _read_proc_environ(pid: int) -> dict[str, str]:
    """Parse ``/proc/<pid>/environ`` into a dict. Empty on any read error
    (race with exit, permission, non-Linux)."""
    out: dict[str, str] = {}
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except (OSError, ValueError):
        return out
    for kv in raw.split(b"\x00"):
        if b"=" in kv:
            k, _, v = kv.partition(b"=")
            out[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    return out


def _scan_hub_adapters() -> list[dict[str, Any]]:
    """Scan ``/proc`` for ``hub_adapter`` processes. Returns ``[{pid, cmdline,
    hub_url}]``. Linux-only (reads ``/proc``); empty list elsewhere."""
    procfs = Path("/proc")
    found: list[dict[str, Any]] = []
    if not procfs.is_dir():
        return found
    for entry in procfs.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            parts = (entry / "cmdline").read_bytes().split(b"\x00")
        except (OSError, ValueError):
            continue
        cmd = " ".join(p.decode("utf-8", "replace") for p in parts if p)
        if not _HUB_ADAPTER_RE.search(cmd):
            continue
        found.append({
            "pid": pid,
            "cmdline": cmd,
            "hub_url": _read_proc_environ(pid).get("AWM_HUB_URL", ""),
        })
    return found


async def _op_services_reap(req: ReapRequest) -> dict[str, Any]:
    """Find and (unless ``dry_run``) SIGTERM→SIGKILL orphaned ``hub_adapter``
    processes that target THIS gateway's origin but hold no live registry lease.

    The durable, single-command replacement for hand-crafted ``ps | awk | kill``
    cleanup of dangling service instances (the orphan storm from repeated
    respawn / ``awm dev shadow`` generations). Discrimination keys off each
    process's ``AWM_HUB_URL`` reduced to ``host:port`` (the documented dev/prod
    discriminator) — so reaping from prod (``:7819``) never touches the dev hub's
    children (``:7821``) and vice-versa. A pid that currently holds a lease (a
    healthy registered base) is never reaped, nor is the gateway's own pid. Kill
    escalation reuses the supervisor's ``kill_pid_group`` (SIGTERM, grace,
    SIGKILL), not a re-rolled signal dance.
    """
    import asyncio as _asyncio

    from awm.gateway.hub import supervisor
    from awm.gateway.hub.lease import get_lease_manager
    from awm.gateway.hub.registry import get_registry

    registry = get_registry()
    lm = get_lease_manager()
    my_origin = _origin_of(supervisor.default_hub_url())
    me = os.getpid()

    # PIDs that currently hold a live lease are healthy bases — never reap them.
    live_pids = {
        rec.backend_pid
        for rec in await registry.list()
        if rec.backend_pid and lm.is_held(rec.service_id)
    }

    candidates: list[dict[str, Any]] = []
    for proc in _scan_hub_adapters():
        if proc["pid"] == me or proc["pid"] in live_pids:
            continue
        if _origin_of(proc["hub_url"]) != my_origin:
            continue
        candidates.append(proc)

    reaped: list[dict[str, Any]] = []
    if not req.dry_run:
        loop = _asyncio.get_event_loop()
        for proc in candidates:
            await loop.run_in_executor(
                None, supervisor.kill_pid_group, proc["pid"])
            reaped.append(proc)

    return {
        "origin": my_origin,
        "dry_run": req.dry_run,
        "count": len(candidates),
        "found": candidates,
        "reaped": reaped,
    }


# ---------------------------------------------------------------------------
# Peer directory — the federation address book. The gateway is a resolver, never
# a relay: these ops manage a name→(edge URL, ssh alias) book; a cross-peer call
# resolves the address here then talks to the peer's edge directly.
# ---------------------------------------------------------------------------


class PeerJoinRequest(BaseModel):
    """Body for ``peer join`` — record a peer node in the address book."""

    name: str
    edge_url: str
    ssh_alias: str | None = None


def _op_peer_join(req: PeerJoinRequest) -> dict[str, Any]:
    """Record (or update) a peer: name → HTTPS edge URL + ssh alias.

    Local to this node's book. Run it on both nodes to make the relationship
    mutual (each side records the other) — the gateway never syncs peers.
    """
    from awm.gateway import peers

    try:
        return {"peer": peers.add(req.name, req.edge_url, req.ssh_alias)}
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


def _op_peer_list() -> dict[str, Any]:
    """List every recorded peer (name, edge URL, ssh alias)."""
    from awm.gateway import peers

    return {"peers": peers.list_all()}


def _op_peer_resolve(name: str) -> dict[str, Any]:
    """Resolve one peer name to its edge URL + ssh alias. 404 if unknown.

    Pure directory lookup — it returns an address and never proxies. Callers use
    the address to reach the peer's edge directly.
    """
    from awm.gateway import peers

    entry = peers.resolve(name)
    if entry is None:
        raise FileNotFoundError(f"unknown peer: {name}")
    return {"peer": entry}


def _op_peer_forget(name: str) -> dict[str, Any]:
    """Remove a peer from this node's book."""
    from awm.gateway import peers

    entry = peers.remove(name)
    if entry is None:
        raise FileNotFoundError(f"unknown peer: {name}")
    return {"forgot": entry}


# ---------------------------------------------------------------------------
# Config contracts — aggregate every opted-in service contract + the gateway's
# own global slot into one settings surface; route writes back to the owner.
# ---------------------------------------------------------------------------

_GLOBAL_KEY = "gateway"


def _global_contract() -> dict[str, Any]:
    """The gateway's own global config slot.

    Intentionally empty for now (no global settings are defined yet). When a
    global setting is chosen, declare it in a Pydantic model here and persist via
    ``awm.persistence.config_service``'s KV — the aggregator + settings page
    already render whatever schema this returns, so only this function changes.
    """
    return {
        "key": _GLOBAL_KEY,
        "title": "Gateway (global)",
        "schema": {"type": "object", "title": "GlobalSettings", "properties": {}},
        "values": {},
        "unavailable": False,
    }


async def _op_config_contracts() -> dict[str, Any]:
    """Every opted-in config contract + live values, for the settings page.

    Walks the hub registry: a service whose ``ready.api`` carries a ``config``
    block has opted in (the default-off marker). Its live values come from an RPC
    ``config_get`` (the service persists them locally); a registered-but-down
    service degrades to ``unavailable`` rather than failing the whole list — its
    schema still shows from the manifest. The gateway's own global slot is always
    appended.
    """
    from awm.gateway.hub import rpc
    from awm.gateway.hub.registry import get_registry

    registry = get_registry()
    out: list[dict[str, Any]] = []
    for rec in await registry.list():
        if rec.kind != "service":
            continue
        cfg = (rec.api or {}).get("config")
        if not cfg:
            continue  # opted out — the default for every current service.
        entry: dict[str, Any] = {
            "key": rec.name,
            "title": cfg.get("title", rec.name),
            "schema": cfg.get("schema", {}),
            "values": None,
            "unavailable": True,
        }
        ch = rpc.get_control(rec.service_id)
        if ch is not None:
            try:
                got = await ch.call("config_get", {})
                if isinstance(got, dict):
                    entry["title"] = got.get("title", entry["title"])
                    entry["schema"] = got.get("schema", entry["schema"])
                    entry["values"] = got.get("values", {})
                    entry["unavailable"] = False
            except Exception:  # noqa: BLE001 — RpcError / timeout / closed channel
                pass  # leave it as unavailable with the manifest schema.
        out.append(entry)
    out.append(_global_contract())
    return {"contracts": out}


class ConfigSetRequest(BaseModel):
    """Body for ``config set`` — write one contract's values to its owner."""

    key: str
    values: dict[str, Any] = {}


async def _op_config_set(req: ConfigSetRequest) -> dict[str, Any]:
    """Persist a contract's values to its owning registrant.

    Routes to the named service's ``config_set`` over RPC (the service validates
    + persists locally), or to the gateway global slot when ``key`` is the global
    slot. Returns the saved contract entry. ``FileNotFoundError`` (→ 404) for an
    unknown key; ``RuntimeError`` (→ 500) when the owning service is down.
    """
    from awm.gateway.hub import rpc
    from awm.gateway.hub.registry import get_registry

    if req.key == _GLOBAL_KEY:
        return _global_contract()  # no global settings to persist yet.

    registry = get_registry()
    rec = registry.get_by_name("service", req.key)
    if rec is None:
        raise FileNotFoundError(f"unknown config contract: {req.key}")
    if not (rec.api or {}).get("config"):
        raise ValueError(f"service {req.key!r} has no config contract")
    ch = rpc.get_control(rec.service_id)
    if ch is None:
        raise RuntimeError(f"service {req.key!r} control channel not open")
    saved = await ch.call("config_set", {"values": req.values or {}})
    entry: dict[str, Any] = {"key": req.key, "unavailable": False}
    if isinstance(saved, dict):
        entry["title"] = saved.get("title", req.key)
        entry["schema"] = saved.get("schema", {})
        entry["values"] = saved.get("values", {})
    return entry


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
        cli_group="", cli_command="",
        output=JsonOutput(), surfaces=_MCP_HTTP,
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
    Operation(
        name="services_reap",
        description="Reap orphaned hub_adapter processes targeting this gateway "
                    "that hold no live lease (--dry-run lists only).",
        service_func=_op_services_reap,
        http_method="POST", http_path="/hub/services/reap",
        cli_group="services", cli_command="reap",
        output=JsonOutput(),
        request_model=ReapRequest,
        params=[Param(
            name="dry_run", type="boolean", required=False, default=False,
            cli_name="--dry-run",
            description="List the orphaned hub_adapters without killing them.",
        )],
        surfaces=_CLI,
    ),
    # --- peer directory (federation address book) -------------------------
    Operation(
        name="peer_join",
        description="Record a peer node (name → HTTPS edge URL + ssh alias) in "
                    "this node's address book. Run on both nodes for a mutual link.",
        service_func=_op_peer_join,
        http_method="POST", http_path="/peers",
        cli_group="peer", cli_command="join",
        output=JsonOutput(),
        request_model=PeerJoinRequest,
        params=[
            Param(name="name", type="string", required=True, cli_type="argument",
                  description="Peer node name (single token; how it is addressed "
                              "in <svc>@<peer>)."),
            Param(name="edge_url", type="string", required=True, cli_type="argument",
                  description="Peer HTTPS edge URL (bare host:port → https://)."),
            Param(name="ssh_alias", type="string", required=False, cli_type="option",
                  description="ssh host for the $AWM_PEER_CRED fetch (default: name)."),
        ],
        surfaces=_CLI,
    ),
    Operation(
        name="peer_list",
        description="List recorded peers (name, edge URL, ssh alias).",
        service_func=_op_peer_list,
        http_method="GET", http_path="/peers",
        cli_group="peer", cli_command="list",
        output=JsonOutput(), surfaces=_CLI,
    ),
    Operation(
        name="peer_resolve",
        description="Resolve a peer name to its edge URL + ssh alias (directory "
                    "lookup only; never proxies).",
        service_func=_op_peer_resolve,
        http_method="GET", http_path="/peers/{name}",
        cli_group="peer", cli_command="resolve",
        output=JsonOutput(),
        params=[Param(name="name", type="string", required=True,
                      location="path", cli_type="argument",
                      description="Peer node name to resolve.")],
        surfaces=_CLI,
    ),
    Operation(
        name="peer_forget",
        description="Remove a peer from this node's address book.",
        service_func=_op_peer_forget,
        http_method="DELETE", http_path="/peers/{name}",
        cli_group="peer", cli_command="forget",
        output=JsonOutput(),
        params=[Param(name="name", type="string", required=True,
                      location="path", cli_type="argument",
                      description="Peer node name to forget.")],
        surfaces=_CLI,
    ),
    # --- config contracts (settings page) ---------------------------------
    Operation(
        name="config_contracts",
        description="List every opted-in service config contract + live values "
                    "(plus the gateway global slot) for the settings page.",
        service_func=_op_config_contracts,
        http_method="GET", http_path="/config/contracts",
        cli_group="config", cli_command="contracts",
        output=JsonOutput(), surfaces=_CLI,
    ),
    Operation(
        name="config_set",
        description="Persist a config contract's values to its owning service "
                    "(or the gateway global slot).",
        service_func=_op_config_set,
        http_method="POST", http_path="/config/set",
        cli_group="config", cli_command="set",
        output=JsonOutput(),
        request_model=ConfigSetRequest,
        params=[
            Param(name="key", type="string", required=True,
                  description="Contract key: a service name, or 'gateway'."),
            Param(name="values", type="object", required=False,
                  description="Field values to persist (full object)."),
        ],
        surfaces=_MCP_HTTP,
    ),
]
