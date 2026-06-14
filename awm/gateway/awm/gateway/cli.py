"""Typer CLI app with auto-start logic."""

from __future__ import annotations

import json
import os
import pathlib
import signal
import subprocess
import sys
import time
from typing import Optional
from urllib.parse import urlsplit

import httpx
import typer

from awm.config import (
    BASE_URL,
    HOST,
    PORT,
    PID_FILE,
    AWM_DIR,
    WORKSPACE_ROOT,
)

app = typer.Typer(name="awm", help="Agentic Workspace Manager", no_args_is_help=True)
dev_app = typer.Typer(help="Dev workflows: shadow packages into the running hub", no_args_is_help=True)

app.add_typer(dev_app, name="dev")

# The `gateway` and `services` command groups are CREATED by the generation
# system (see `_wire_generated_commands` below): each migratable control op is
# one Operation in GATEWAY_OPERATIONS, and `register_cli_commands` emits its CLI
# command into the right group. The few ops that can't be declarative
# (init/serve/stop/refresh/register; services list/start with their offline
# fallback / --all) are hand-authored and ATTACH to those same group instances.
# Populated at import time after the `_api` helper is defined.
gateway_app: typer.Typer
services_app: typer.Typer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _server_running() -> bool:
    """Check if the server is up."""
    try:
        r = httpx.get(f"{BASE_URL}/status", timeout=2)
        return r.status_code == 200
    except (httpx.ConnectError, httpx.ReadTimeout):
        return False


def _ensure_server():
    """Start the server in background if it's not running."""
    if _server_running():
        return
    typer.echo("Starting AWM server...")
    AWM_DIR.mkdir(parents=True, exist_ok=True)
    log_path = AWM_DIR / "awm.log"
    log_file = open(log_path, "a")
    env = os.environ.copy()
    env["AWM_WORKSPACE"] = str(WORKSPACE_ROOT)
    proc = subprocess.Popen(
        [sys.executable, "-m", "awm.gateway", "gateway", "serve"],
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
        env=env,
    )
    # Wait up to 3s for server to come up
    for _ in range(30):
        time.sleep(0.1)
        if _server_running():
            typer.echo(f"Server started (PID {proc.pid}) on {HOST}:{PORT}")
            return
    typer.echo(f"Warning: Server may not have started. Check {log_path}", err=True)


def _api(method: str, path: str, **kwargs) -> httpx.Response:
    """Make an API call, auto-starting the server if needed."""
    _ensure_server()
    url = f"{BASE_URL}{path}"
    r = httpx.request(method, url, timeout=30, **kwargs)
    return r


def _local_api(method: str, path: str, **kwargs) -> httpx.Response:
    """Hit an endpoint on the local awm listener (loopback HTTP, no auth)."""
    r = httpx.request(method, f"{BASE_URL}{path}", timeout=30, **kwargs)
    return r


# ---------------------------------------------------------------------------
# Generated control-plane commands
# ---------------------------------------------------------------------------
# The gateway control plane (status / restart / mcp-sync / hub list+deregister /
# services lifecycle) generates its CLI from GATEWAY_OPERATIONS — one Operation
# per op, the SAME source that drives the MCP tool + HTTP route. The generator
# owns the `gateway` / `services` group instances; the hand-authored commands
# below attach to them. Generated commands round-trip through `_api`, which
# auto-starts the gateway — so they inherit auto-start like the rest.
from awm.gateway.gateway_ops import GATEWAY_OPERATIONS  # noqa: E402
from awm.gateway.operations import register_cli_commands  # noqa: E402

_generated_groups = register_cli_commands(app, GATEWAY_OPERATIONS, api_func=_api)
gateway_app = _generated_groups["gateway"]
services_app = _generated_groups["services"]


# ---------------------------------------------------------------------------
# Gateway lifecycle (hand-authored — local bootstrap / process control, no
# running-gateway HTTP to generate from). Attached to the generated `gateway`
# group so they sit alongside the generated status/restart/list/deregister.
# ---------------------------------------------------------------------------

@gateway_app.command()
def init():
    """Bootstrap the workspace directory layout.

    Per-service DBs are created lazily by each feature service on first use,
    so the gateway no longer opens or initializes a shared database here.
    """
    AWM_DIR.mkdir(parents=True, exist_ok=True)

    # Ensure workspace directories exist
    for d in ["data/reference", "projects", "main"]:
        (WORKSPACE_ROOT / d).mkdir(parents=True, exist_ok=True)

    typer.echo(f"Initialized AWM at {AWM_DIR}")
    typer.echo(f"Workspace: {WORKSPACE_ROOT}")


@gateway_app.command()
def serve():
    """Run the AWM server in the foreground."""
    from awm.gateway.server import run_server
    run_server()


@gateway_app.command()
def stop():
    """Stop the AWM server."""
    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        try:
            os.kill(pid, signal.SIGTERM)
            typer.echo(f"Sent SIGTERM to server (PID {pid})")
        except ProcessLookupError:
            typer.echo("Server process not found (stale PID file)")
            PID_FILE.unlink()
    else:
        typer.echo("No PID file found — server may not be running")


@gateway_app.command()
def refresh():
    """Restart the server to pick up source changes."""
    # Stop the running server if present
    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        try:
            os.kill(pid, signal.SIGTERM)
            typer.echo(f"Stopping server (PID {pid})...")
            # Wait up to 5s for process to exit
            for _ in range(50):
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)  # probe — raises if gone
                except ProcessLookupError:
                    break
        except ProcessLookupError:
            typer.echo("Server process already gone (stale PID file)")
        if PID_FILE.exists():
            PID_FILE.unlink(missing_ok=True)
    elif _server_running():
        typer.echo("Warning: server is running but no PID file found — cannot stop it")
        raise typer.Exit(1)

    # Start a fresh server
    _ensure_server()
    typer.echo("Server refreshed.")


# ---------------------------------------------------------------------------
# Register an external bundle/service and hold its WS lease (hand-authored:
# stateful — it POSTs then holds the lease open, so it can't be a declarative
# request/response Operation). Lives under `awm gateway register`.
# ---------------------------------------------------------------------------

@gateway_app.command("register")
def gateway_register(
    name: str = typer.Option(..., "--name", help="Service name (must be unique)"),
    prefix: str = typer.Option(..., "--prefix", help="URL prefix to claim (e.g. /demo)"),
    url: str | None = typer.Option(
        None, "--url",
        help="Local URL the service listens on (kind=url). "
             "Mutually exclusive with --dir.",
    ),
    dir: str | None = typer.Option(
        None, "--dir",
        help="Local directory to serve at the prefix (kind=static). "
             "Mutually exclusive with --url.",
    ),
    entry: str | None = typer.Option(
        None, "--entry",
        help="Relative path to the ESM entry script. Used for the auto-shell "
             "when --dir has no index.html.",
    ),
    css: list[str] = typer.Option(
        None, "--css",
        help="Relative path to a stylesheet (repeatable). "
             "Injected into the auto-shell.",
    ),
    mount_id: str = typer.Option(
        "app", "--mount-id",
        help="DOM id of the mount node in the auto-shell.",
    ),
):
    """Register a service and hold a WS lease until interrupted.

    On Ctrl-C the lease closes and the hub evicts the registration on
    the next event-loop tick. Re-running this command after eviction
    re-registers from scratch.

    Two flavours:

    \b
      --url http://127.0.0.1:5173        # forward HTTP/WS to a local process
      --dir ./dist [--entry main.js …]   # serve a built directory at the prefix
    """
    import asyncio as _asyncio
    import json as _json
    import ssl as _ssl

    import websockets as _ws

    if bool(url) == bool(dir):
        typer.echo("exactly one of --url or --dir must be provided", err=True)
        raise typer.Exit(2)
    if url and (entry or css or mount_id != "app"):
        typer.echo("--entry/--css/--mount-id only apply with --dir", err=True)
        raise typer.Exit(2)

    if dir:
        from pathlib import Path as _Path
        dir_abs = str(_Path(dir).expanduser().resolve())
        payload = {
            "name": name,
            "prefix": prefix,
            "static": {
                "dir": dir_abs,
                "entry": entry,
                "css": list(css or []),
                "mount_id": mount_id,
            },
        }
        summary = f"dir={dir_abs}"
    else:
        payload = {"name": name, "prefix": prefix, "url": url}
        summary = f"url={url}"

    try:
        r = httpx.post(f"{BASE_URL}/hub/register", json=payload, timeout=10)
    except httpx.HTTPError as exc:
        typer.echo(f"could not reach hub at {BASE_URL}: {exc}", err=True)
        raise typer.Exit(1)
    if r.status_code >= 400:
        typer.echo(f"register failed ({r.status_code}): {r.text}", err=True)
        raise typer.Exit(1)
    body = r.json()
    service_id = body["service_id"]
    lease_path = body["lease_ws_path"]
    typer.echo(f"registered {name} → {summary} (id={service_id})")
    typer.echo(f"holding lease at ws://...{lease_path} (Ctrl-C to evict)")

    ws_url = f"{BASE_URL.replace('http://', 'ws://')}{lease_path}"

    async def _hold():
        async with _ws.connect(ws_url, max_size=None, open_timeout=10) as wsconn:
            # First frame is {"type":"ready",...} — print it then idle.
            try:
                first = await wsconn.recv()
                try:
                    typer.echo(f"hub: {_json.loads(first)}")
                except Exception:
                    typer.echo(f"hub: {first!r}")
            except Exception:
                pass
            # Idle forever; the hub's eviction is triggered by close.
            try:
                async for _ in wsconn:
                    pass
            except _ws.WebSocketException:
                return

    try:
        _asyncio.run(_hold())
    except KeyboardInterrupt:
        typer.echo("lease closed — service evicted")


# ---------------------------------------------------------------------------
# Services: list + start are hand-authored (offline fallback / --all); stop /
# restart / enable / disable are GENERATED from GATEWAY_OPERATIONS. All six
# share the `services` group the generator created.
# ---------------------------------------------------------------------------
#
# A service is just a folder under awm/services/<name>/ with a run.sh. The
# running gateway discovers, starts, and supervises it; these commands are a
# thin control surface over its /hub/services/* endpoints (the gateway is
# authoritative for discovery root + enable-state + journal).


def _require_server() -> None:
    if not _server_running():
        typer.echo("gateway is not running (start it with `awm gateway serve` "
                   "or `awm dev start`)", err=True)
        raise typer.Exit(1)


def _services_action(name: str, action: str) -> dict:
    _require_server()
    r = _local_api("POST", f"/hub/services/{name}/{action}")
    if r.status_code >= 400:
        typer.echo(f"{action} {name} failed ({r.status_code}): {r.text}", err=True)
        raise typer.Exit(1)
    return r.json()


@services_app.command("list")
def services_list():
    """List discovered services with their enable + running state."""
    if not _server_running():
        # Best-effort offline view straight from local discovery.
        from awm.gateway.hub import discovery
        specs = discovery.discover_services()
        if not specs:
            typer.echo("no services discovered under awm/services/*")
            return
        for s in specs:
            typer.echo(f"{s.name:14} enabled={s.enabled}  (gateway down)")
        return
    r = _local_api("GET", "/hub/services/discovered")
    if r.status_code >= 400:
        typer.echo(f"error ({r.status_code}): {r.text}", err=True)
        raise typer.Exit(1)
    rows = r.json().get("services", [])
    if not rows:
        typer.echo("no services discovered under awm/services/*")
        return
    typer.echo(f"{'NAME':14} {'ENABLED':8} {'RUNNING':8} STATUS")
    for row in rows:
        typer.echo(f"{row['name']:14} {str(row['enabled']):8} "
                   f"{str(row['running']):8} {row['status']}")


@services_app.command("start")
def services_start(
    name: Optional[str] = typer.Argument(
        None, help="Service to start (omit when using --all)"),
    all_: bool = typer.Option(False, "--all", help="Start every enabled service"),
):
    """Start a stopped service (idempotent — a running one is left alone)."""
    _require_server()
    if all_:
        r = _local_api("GET", "/hub/services/discovered")
        names = [s["name"] for s in r.json().get("services", []) if s["enabled"]]
    elif name:
        names = [name]
    else:
        typer.echo("give a service name or --all", err=True)
        raise typer.Exit(1)
    for n in names:
        typer.echo(json.dumps(_services_action(n, "start")))


# `services stop / restart / enable / disable` are generated from
# GATEWAY_OPERATIONS (see gateway_ops.py) — no hand-rolled commands here.


# ---------------------------------------------------------------------------
# `awm dev shadow <path>` — overlay one service (exec its run.sh with
# AWM_SERVICE_OVERLAY=1) or a built page onto the running hub; Ctrl-C pops
# the overlay (no respawn — base traffic resumes). The page-register +
# lease-holding helpers below are shared with the dev shadow flow.
# ---------------------------------------------------------------------------


async def _hold_one_lease(name: str, lease_path: str) -> None:
    """Open the lease WS and idle until close. Used by `awm dev shadow`
    (one lease per page overlay/base it brings up)."""
    import websockets as _ws

    ws_url = f"{BASE_URL.replace('http://', 'ws://')}{lease_path}"
    async with _ws.connect(ws_url, max_size=None, open_timeout=10) as wsconn:
        try:
            first = await wsconn.recv()
            try:
                typer.echo(f"hub[{name}]: {json.loads(first)}")
            except Exception:
                typer.echo(f"hub[{name}]: {first!r}")
        except Exception:
            pass
        try:
            async for _ in wsconn:
                pass
        except _ws.WebSocketException:
            return


def _post_register(payload: dict) -> dict:
    """POST /hub/register with the given payload, return parsed body
    or typer.Exit(1) on error."""
    try:
        r = httpx.post(f"{BASE_URL}/hub/register", json=payload, timeout=15)
    except httpx.HTTPError as exc:
        typer.echo(f"could not reach hub at {BASE_URL}: {exc}", err=True)
        raise typer.Exit(1)
    if r.status_code >= 400:
        typer.echo(f"register failed ({r.status_code}): {r.text}", err=True)
        raise typer.Exit(1)
    return r.json()


def _post_page_register(name: str, prefix: str, dir_: str) -> dict:
    payload = {
        "name": name,
        "prefix": prefix,
        "page": {"dir": dir_},
    }
    return _post_register(payload)


def _read_prefix_txt(pkg_dir: pathlib.Path, default: str) -> str:
    f = pkg_dir / "prefix.txt"
    if f.is_file():
        text = f.read_text(encoding="utf-8").strip()
        if text:
            return text if text.startswith("/") else "/" + text
    return default


def _dev_run_sh() -> pathlib.Path:
    """Locate this worktree's dev sandbox script: walk up from CWD to the
    worktree root, then ``awm/gateway/dev/run.sh``."""
    cur = pathlib.Path.cwd().resolve()
    for parent in [cur, *cur.parents]:
        cand = parent / "awm" / "gateway" / "dev" / "run.sh"
        if cand.is_file():
            return cand
    typer.echo("no awm/gateway/dev/run.sh above the current directory "
               "(run this from inside a worktree)", err=True)
    raise typer.Exit(1)


def _dev_exec(action: str) -> None:
    rc = subprocess.run(["bash", str(_dev_run_sh()), action]).returncode
    if rc != 0:
        raise typer.Exit(rc)


def _print_dev_result(payload) -> None:
    """Print a dev-service lifecycle result (the {action,rc,stdout,stderr} dict
    a handler returned) as if run.sh had run on this terminal."""
    if not isinstance(payload, dict):
        typer.echo(payload)
        return
    out = payload.get("stdout") or ""
    err = payload.get("stderr") or ""
    if out:
        typer.echo(out.rstrip("\n"))
    if err:
        typer.echo(err.rstrip("\n"), err=True)
    if "error" in payload:
        typer.echo(f"dev: {payload['error']}", err=True)


def _dev_op(op: str, local: bool) -> None:
    """Run a dev lifecycle op (start/stop/restart/seed/status).

    Default: invoke the ``dev`` service tool on the hub (``BASE_URL`` — prod
    :7819 unless overridden). When a dev sandbox is live its self-shadow overlay
    serves the call against *its* worktree, so ``awm dev`` reaches the sandbox
    through prod without retargeting. Falls back to running ``run.sh`` locally in
    the current worktree when ``--local`` is given, the hub is unreachable, no
    ``dev`` service is registered, or the hub's ``dev`` base is inert (no sandbox
    shadowing it). ``start`` always runs locally — it bootstraps the sandbox that
    does the shadowing, so there is nothing on the bus to route it yet.
    """
    if local or op == "start":
        _dev_exec(op)
        return
    try:
        r = _local_api("POST", "/invoke", json={"name": f"dev_{op}", "args": {}})
    except httpx.HTTPError as exc:
        typer.echo(f"dev: hub unreachable ({exc}); running locally", err=True)
        _dev_exec(op)
        return
    if r.status_code == 404:
        typer.echo("dev: no `dev` service on the hub; running locally", err=True)
        _dev_exec(op)
        return
    if r.status_code >= 400:
        typer.echo(f"dev: hub error ({r.status_code}): {r.text}", err=True)
        raise typer.Exit(1)
    payload = r.json().get("result")
    if isinstance(payload, dict) and payload.get("inert"):
        typer.echo(f"dev: {payload.get('reason', 'prod base is inert')}; "
                   f"running locally", err=True)
        _dev_exec(op)
        return
    _print_dev_result(payload)
    rc = payload.get("rc") if isinstance(payload, dict) else None
    if rc not in (None, 0):
        raise typer.Exit(int(rc))


_LOCAL_OPT = typer.Option(
    False, "--local", "-l",
    help="Run run.sh in THIS worktree directly instead of routing through the "
         "hub's dev service (the self-shadow path).",
)


@dev_app.command("start")
def dev_start(local: bool = _LOCAL_OPT):
    """Start this worktree's dev sandbox (gateway + bootstrapped services).

    Always local — `start` bootstraps the sandbox whose dev service then
    self-shadows prod, so there is nothing on the bus to route it through yet.
    """
    _dev_op("start", local)


@dev_app.command("stop")
def dev_stop(local: bool = _LOCAL_OPT):
    """Stop the dev sandbox (services + gateway). Routes through the hub's dev
    service so a live sandbox stops itself (overlay drops, prod base resumes)."""
    _dev_op("stop", local)


@dev_app.command("restart")
def dev_restart(local: bool = _LOCAL_OPT):
    """Restart the dev sandbox."""
    _dev_op("restart", local)


@dev_app.command("seed")
def dev_seed(local: bool = _LOCAL_OPT):
    """(Re)seed the dev sandbox DB without restarting."""
    _dev_op("seed", local)


@dev_app.command("status")
def dev_status(local: bool = _LOCAL_OPT):
    """Show what the dev sandbox is running (served by the live sandbox via the
    hub's dev overlay; falls back to this worktree's run.sh)."""
    _dev_op("status", local)


def _shadow_dev_pythonpath(service_dir: pathlib.Path) -> str:
    """Build a DEV_PYTHONPATH of the shadowed worktree's dist roots so the
    overlay runs THAT worktree's code, not the installed tree. Walk up to the
    worktree root (the dir with AGENTS.md), then collect every dist root that
    has an inner ``awm/`` package — same shape the dev sandbox uses."""
    root = None
    for parent in [service_dir, *service_dir.parents]:
        if (parent / "AGENTS.md").is_file():
            root = parent
            break
    if root is None:
        return ""
    candidates = [root / "awm" / "gateway"]
    for sub in ("service_components", "services"):
        d = root / "awm" / sub
        if d.is_dir():
            candidates.extend(sorted(d.iterdir()))
    return ":".join(str(c) for c in candidates if (c / "awm").is_dir())


@dev_app.command("shadow")
def dev_shadow(
    targets: list[str] = typer.Argument(
        ...,
        help="One or more targets to bring up on the hub in ONE process. Each is "
             "either 'pages/<name>' (a built page from awm/pages/<name>/dist) or a "
             "path to a service folder / its run.sh (e.g. awm/services/agents). "
             "Example: awm dev shadow --port 7821 pages/agent awm/services/agents "
             "awm/services/tts awm/services/ptt",
    ),
    port: int = typer.Option(
        7821, "--port", "-p",
        help="Hub port to bring these up on. Defaults to 7821 (the dev sandbox). "
             "The CLI otherwise targets AWM_PORT (prod 7819) — set this so you "
             "never shadow onto prod by accident.",
    ),
    name: Optional[str] = typer.Option(
        None, "--name",
        help="Override the overlay/base name (single target only).",
    ),
):
    """Bring our worktree's pages + services up on a running hub (default: dev :7821).

    Everything comes up in ONE foreground process; Ctrl-C tears the whole stack
    down (page overlays popped, service subprocesses SIGTERM'd, any base we created
    evicted, dev's own bases resume).

    Each target auto-selects base-vs-overlay against what the hub already serves:

    - Page ('pages/<name>'): serves ``awm/pages/<name>/dist`` at ``/ui/<name>``. If a
      page base already serves that prefix it's pushed as an overlay; otherwise it
      registers as a fresh page base.
    - Service (a path under ``awm/services/``): execs the folder's ``run.sh`` with the
      dev ``PYTHONPATH`` so it runs THIS worktree's code. If ``/svc/<name>`` already has
      a base it registers as an overlay (``AWM_SERVICE_OVERLAY=1``); otherwise the
      adapter self-registers as the base. A base created this way is NOT journaled —
      it won't respawn if the hub restarts mid-session.
    """
    import asyncio as _asyncio

    global BASE_URL
    BASE_URL = f"http://{HOST}:{port}"

    if name and len(targets) > 1:
        typer.echo("--name only applies to a single target", err=True)
        raise typer.Exit(1)
    _require_server()

    # One registry snapshot drives the base-vs-overlay decision for every target:
    # a service base = a kind=service record at /svc/<name> that isn't itself an
    # overlay; a page base = a kind=page record at that prefix.
    r = _local_api("GET", "/hub/services")
    svcs = r.json().get("services", []) if r.status_code < 400 else []
    service_bases = {s["name"] for s in svcs
                     if s.get("kind") == "service" and not s.get("is_overlay")}
    page_prefixes = {s["prefix"] for s in svcs
                     if s.get("kind") == "page" and not s.get("is_overlay")}

    leases: list[tuple[str, str]] = []   # page leases this CLI holds
    spawned_pids: list[int] = []         # service subprocesses we own

    for target in targets:
        if target.startswith("pages/"):
            lease = _shadow_page_target(target.split("/", 1)[1], name, page_prefixes)
            if lease:
                leases.append(lease)
        else:
            pid = _shadow_service_target(target, name, service_bases)
            if pid:
                spawned_pids.append(pid)

    if not leases and not spawned_pids:
        typer.echo("nothing brought up", err=True)
        raise typer.Exit(1)

    async def _hold_all():
        await _asyncio.gather(*(_hold_one_lease(n, p) for n, p in leases))
    try:
        if leases:
            typer.echo(f"holding {len(leases)} page lease(s) + {len(spawned_pids)} "
                       f"service(s) on :{port}; Ctrl-C to tear down…")
            _asyncio.run(_hold_all())
        else:
            typer.echo(f"{len(spawned_pids)} service(s) running on :{port} "
                       f"(pids={spawned_pids}); Ctrl-C to tear down…")
            signal.pause()
    except KeyboardInterrupt:
        typer.echo("tearing down…")
    finally:
        for pid in spawned_pids:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    typer.echo("torn down — base traffic resumes")


def _shadow_page_target(
    name: str, override_name: Optional[str], page_prefixes: set[str],
) -> Optional[tuple[str, str]]:
    """Bring up ``awm/pages/<name>/dist`` on ``/ui/<name>``.

    Overlay if a page base already serves the prefix, else register a fresh page
    base. Returns ``(label, lease_ws_path)`` for the caller to hold, or ``None`` on
    skip. The CLI holds the lease; closing it pops the overlay / evicts the base.
    """
    pkg_dir = (pathlib.Path.cwd() / "awm" / "pages" / name).expanduser().resolve()
    if not pkg_dir.is_dir():
        typer.echo(f"pages/{name}: {pkg_dir} not a directory", err=True)
        return None
    dist = pkg_dir / "dist"
    if not dist.is_dir():
        typer.echo(f"pages/{name}: no dist/ — build first", err=True)
        return None
    prefix = _read_prefix_txt(pkg_dir, f"/ui/{name}")

    if prefix in page_prefixes:
        worktree = pkg_dir.parents[2].name   # <root>/awm/pages/<name> → <root>
        shadow_name = override_name or f"shadow:{name}:{worktree}"
        payload = {"name": shadow_name, "prefix": prefix, "page": {"dir": str(dist)}}
        try:
            r = httpx.post(f"{BASE_URL}/hub/shadow/register", json=payload, timeout=15)
        except httpx.HTTPError as exc:
            typer.echo(f"pages/{name}: hub unreachable: {exc}", err=True)
            return None
        if r.status_code >= 400:
            typer.echo(f"pages/{name}: shadow register failed "
                       f"({r.status_code}): {r.text}", err=True)
            return None
        body = r.json()
        typer.echo(f"page  {name:16} → overlay {prefix}")
        return (f"pages/{name}", body["lease_ws_path"])

    # No base serves this prefix yet — register a fresh page base.
    try:
        body = _post_page_register(override_name or name, prefix, str(dist))
    except typer.Exit:
        return None
    typer.echo(f"page  {name:16} → base    {prefix}")
    return (f"pages/{name}", body["lease_ws_path"])


def _shadow_service_target(
    target: str, override_name: Optional[str], service_bases: set[str],
) -> Optional[int]:
    """Exec a service folder's ``run.sh`` against the hub with this worktree's code.

    Overlay if ``/svc/<name>`` already has a base, else self-register as the base
    (the adapter's ``AWM_SERVICE_ID``-unset path). Returns the spawned pid, or
    ``None`` on skip. ``DEV_PYTHONPATH`` makes the process load this worktree's tree.
    """
    p = pathlib.Path(target).expanduser().resolve()
    if p.is_dir():
        service_dir, run_sh = p, p / "run.sh"
    elif p.name == "run.sh" and p.is_file():
        service_dir, run_sh = p.parent, p
    else:
        typer.echo(f"{target}: expected a service folder containing run.sh "
                   f"(or the run.sh itself); got {p}", err=True)
        return None
    if not run_sh.is_file():
        typer.echo(f"{service_dir}: no run.sh — not a service folder", err=True)
        return None

    base_name = override_name or service_dir.name
    is_overlay = base_name in service_bases

    env = os.environ.copy()
    env["AWM_HUB_URL"] = BASE_URL
    # Pin AWM_PORT to the shadow hub's port too, so anything this service spawns
    # (e.g. a shadowed `agents` service's child agents, whose awm-mcp reads
    # config.PORT) targets THIS sandbox — not prod :7819, the import-time default.
    _port = urlsplit(BASE_URL).port
    if _port:
        env["AWM_PORT"] = str(_port)
    env.setdefault("AWM_ENV", "awm")
    if is_overlay:
        env["AWM_SERVICE_NAME"] = f"{base_name}-shadow"
        env["AWM_SERVICE_PREFIX"] = f"/svc/{base_name}"
        env["AWM_SERVICE_OVERLAY"] = "1"
    else:
        # Self-register as a fresh base: a unique name, no overlay flag, no
        # pre-assigned id (the adapter POSTs /hub/service/register itself).
        env["AWM_SERVICE_NAME"] = base_name
        for k in ("AWM_SERVICE_ID", "AWM_SERVICE_OVERLAY", "AWM_SERVICE_PREFIX"):
            env.pop(k, None)
    pp = _shadow_dev_pythonpath(service_dir)
    if pp:
        env["DEV_PYTHONPATH"] = pp

    if is_overlay:
        typer.echo(f"svc   {base_name:16} → overlay /svc/{base_name}")
    else:
        typer.echo(f"svc   {base_name:16} → base    /svc/{base_name}"
                   f"  (not journaled — won't respawn on hub restart)")
    proc = subprocess.Popen(
        ["bash", str(run_sh)], cwd=str(service_dir), env=env,
        start_new_session=True,
    )
    return proc.pid
