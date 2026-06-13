"""Typer CLI app with auto-start logic."""

from __future__ import annotations

import json
import os
import pathlib
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

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
hub_app = typer.Typer(help="Service hub: register + lease external services", no_args_is_help=True)
packages_app = typer.Typer(help="Packages: generate manifests + sync packages/{services,pages}/ with the hub", no_args_is_help=True)
dev_app = typer.Typer(help="Dev workflows: shadow packages into the running hub", no_args_is_help=True)
services_app = typer.Typer(help="Feature services: list / start / stop / restart / enable / disable the folders the gateway runs", no_args_is_help=True)

context_app = typer.Typer(help="Scope context: emit .awm/context.md for harness SessionStart hooks", no_args_is_help=True)

app.add_typer(context_app, name="context")
app.add_typer(hub_app, name="hub")
app.add_typer(packages_app, name="packages")
app.add_typer(dev_app, name="dev")
app.add_typer(services_app, name="services")


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
        [sys.executable, "-m", "awm", "serve"],
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


def _print_json(r: httpx.Response):
    """Print response, handling errors."""
    if r.status_code >= 400:
        typer.echo(f"Error ({r.status_code}): {r.text}", err=True)
        raise typer.Exit(1)
    data = r.json()
    import json
    typer.echo(json.dumps(data, indent=2))


def _local_api(method: str, path: str, **kwargs) -> httpx.Response:
    """Hit an endpoint on the local awm listener (loopback HTTP, no auth)."""
    r = httpx.request(method, f"{BASE_URL}{path}", timeout=30, **kwargs)
    return r


# ---------------------------------------------------------------------------
# Top-level commands
# ---------------------------------------------------------------------------

@app.command()
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


@app.command()
def serve():
    """Run the AWM server in the foreground."""
    from awm.gateway.server import run_server
    run_server()


@app.command()
def status():
    """Show server health + scopes summary."""
    r = _api("GET", "/status")
    _print_json(r)


@app.command()
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


@app.command()
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


@app.command()
def restart():
    """Restart the awm core service via systemd (user unit).

    The long-running FastAPI core runs under ``systemctl --user`` as
    ``awm.service``. Restarting this way is transparent to MCP clients —
    the stdio proxy reconnects on the next tool call.
    """
    try:
        r = _api("POST", "/restart")
        data = r.json()
        typer.echo(data.get("message", "awm core restarting."))
    except Exception:
        # Core is unreachable — call systemctl directly as fallback.
        result = subprocess.run(
            ["systemctl", "--user", "restart", "awm.service"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            typer.echo(f"systemctl restart failed: {result.stderr.strip() or result.stdout.strip()}", err=True)
            typer.echo("Hint: install the unit at ~/.config/systemd/user/awm.service first "
                       "(see awm/gateway/deploy/awm.service).", err=True)
            raise typer.Exit(result.returncode)
        typer.echo("awm core restarted (direct systemctl fallback). MCP clients reconnect on next tool call.")


# ---------------------------------------------------------------------------
# Scope context — emit .awm/context.md for harness SessionStart hooks
# ---------------------------------------------------------------------------


@context_app.command("emit")
def context_emit(
    cwd: Path = typer.Option(
        Path.cwd(), "--cwd",
        help="Worktree root to resolve context layers from (walks up to find WORKSPACE.md)",
    ),
):
    """Emit the 3-tier context block for harness SessionStart hooks.

    Walks up from ``cwd`` to find the workspace root (marked by
    ``WORKSPACE.md``) and emits, in general → specific order:

    1. ``<workspace-context path="…WORKSPACE.md">…</workspace-context>``
       — universal structural orientation for every scope agent.
    2. ``<agents-context path="AGENTS.md">…</agents-context>`` — only if
       ``<cwd>/AGENTS.md`` exists AND ``cwd != workspace_root``. The cwd-only
       rule + workspace-root exclusion routes awm-internal docs to awm-dev
       scopes (which share .bare with the workspace, so the file IS the same)
       and lets non-awm projects opt in by placing their own ``AGENTS.md`` at
       the scope root, without ever leaking the workspace-level awm-internal
       file into non-awm contexts.
    3. ``<scope-context path=".awm/context.md">…</scope-context>`` — the
       scope's per-task ritual brief.

    Designed for the Claude Code ``hooks.SessionStart`` additionalContext.
    If ``cwd`` is outside any workspace (no ``WORKSPACE.md`` upstream), exits
    silently with no output — never errors, so the hook never fails. Missing
    individual layers are skipped silently. History and artifacts are
    intentionally NOT emitted (too large; load-on-demand only).
    """
    cwd = cwd.resolve()

    # Walk to the OUTERMOST WORKSPACE.md, not the innermost. The .bare
    # worktree-sharing topology means a scope worktree under projects/awm/*
    # has its own WORKSPACE.md copy at root (committed on the dev branch);
    # the workspace's WORKSPACE.md lives at agentic_workspace/. Picking the
    # outermost keeps workspace_root pinned at the true workspace, so the
    # `cwd != workspace_root` guard below correctly emits the agents block
    # for awm-dev scopes.
    workspace_root: Path | None = None
    for p in (cwd, *cwd.parents):
        if (p / "WORKSPACE.md").is_file():
            workspace_root = p
    if workspace_root is None:
        raise typer.Exit(code=0)

    _emit_context_block("workspace-context", workspace_root / "WORKSPACE.md", base=cwd)

    agents_md = cwd / "AGENTS.md"
    if cwd != workspace_root and agents_md.is_file():
        _emit_context_block("agents-context", agents_md, base=cwd)

    scope_ctx = cwd / ".awm" / "context.md"
    if scope_ctx.is_file():
        _emit_context_block("scope-context", scope_ctx, base=cwd)


def _emit_context_block(tag: str, path: Path, *, base: Path) -> None:
    body = path.read_text()
    rel = path.relative_to(base) if path.is_relative_to(base) else path
    typer.echo(f'<{tag} path="{rel}">')
    typer.echo(body if body.endswith("\n") else body + "\n", nl=False)
    typer.echo(f"</{tag}>")


# ---------------------------------------------------------------------------
# Service hub — register a foreground process as a routed service
# ---------------------------------------------------------------------------

@hub_app.command("register")
def hub_register(
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


@hub_app.command("list")
def hub_list():
    """List currently registered services."""
    r = _local_api("GET", "/hub/services")
    if r.status_code >= 400:
        typer.echo(f"error ({r.status_code}): {r.text}", err=True)
        raise typer.Exit(1)
    _print_json(r)


@hub_app.command("deregister")
def hub_deregister(
    name: str = typer.Argument(..., help="Service name to evict"),
    kind: str = typer.Option(None, "--kind", help="Disambiguate if the name exists across multiple kinds (page|service|url|static)"),
):
    """Force-evict a service by name (independent of its lease holder)."""
    path = f"/hub/services/{name}"
    if kind:
        path += f"?kind={kind}"
    r = _local_api("DELETE", path)
    if r.status_code >= 400:
        typer.echo(f"error ({r.status_code}): {r.text}", err=True)
        raise typer.Exit(1)
    _print_json(r)


# ---------------------------------------------------------------------------
# Services: list / start / stop / restart / enable / disable
# ---------------------------------------------------------------------------
#
# A service is just a folder under awm/services/<name>/ with a run.sh. The
# running gateway discovers, starts, and supervises it; these commands are a
# thin control surface over its /hub/services/* endpoints (the gateway is
# authoritative for discovery root + enable-state + journal).


def _require_server() -> None:
    if not _server_running():
        typer.echo("gateway is not running (start it with `awm serve` or "
                   "`awm dev start`)", err=True)
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


@services_app.command("stop")
def services_stop(name: str = typer.Argument(..., help="Service to stop")):
    """Stop a running service (evict + kill + drop journal)."""
    typer.echo(json.dumps(_services_action(name, "stop")))


@services_app.command("restart")
def services_restart(name: str = typer.Argument(..., help="Service to restart")):
    """Stop then start a service."""
    _services_action(name, "stop")
    typer.echo(json.dumps(_services_action(name, "start")))


@services_app.command("enable")
def services_enable(name: str = typer.Argument(..., help="Service to enable")):
    """Enable a service (persists across restart) and start it now."""
    typer.echo(json.dumps(_services_action(name, "enable")))


@services_app.command("disable")
def services_disable(name: str = typer.Argument(..., help="Service to disable")):
    """Disable a service (stays down across restart) and stop it now."""
    typer.echo(json.dumps(_services_action(name, "disable")))




# ---------------------------------------------------------------------------
# Packages: gen / sync / list / register (and `awm dev shadow`)
# ---------------------------------------------------------------------------
#
# `awm packages gen <repo_root>` — write generated package.json (+ per-page
#   vite.config.ts) from the packages/{components,pages}/<name>/ layout
#   and a regex scan of each package's src/ for @awm/<x> imports.
#   Idempotent; CI gates on `git diff --quiet` after a fresh run.
#
# `awm packages sync <repo_root>` — register every packages/services/<name>
#   (kind="service") and packages/pages/<name> (kind="page") with the hub.
#   Holds N concurrent leases until Ctrl-C. Services do not get a port;
#   their start.sh is invoked with AWM_HUB_URL in env (no auth) so they can
#   call /hub/service/register themselves.
#
# `awm dev shadow <path>` — overlay one service (exec its run.sh with
#   AWM_SERVICE_OVERLAY=1) or a built page onto the running hub; Ctrl-C pops
#   the overlay (no respawn — base traffic resumes).


async def _hold_one_lease(name: str, lease_path: str) -> None:
    """Open the lease WS and idle until close. Used by packages sync (N
    concurrent leases) and dev shadow (per-overlay lease)."""
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


@packages_app.command("gen")
def packages_gen(
    repo_root: str = typer.Argument(
        ".",
        help="Workspace root containing packages/. Defaults to cwd.",
    ),
):
    """Generate per-package package.json + per-page vite.config.ts from the
    packages/{components,pages}/<name>/ layout. Run before npm install."""
    from awm.gateway.packages import gen as _gen
    root = pathlib.Path(repo_root).expanduser().resolve()
    counters = _gen.run(root)
    typer.echo(json.dumps(counters, indent=2))


def _packages_walk(repo_root: pathlib.Path) -> tuple[list[pathlib.Path],
                                                     list[pathlib.Path]]:
    """Return (service_dirs, page_dirs) under repo_root/packages/. Skips
    any subdir name starting with '_' (e.g. _shared/)."""
    pkgs = repo_root / "packages"
    services: list[pathlib.Path] = []
    pages: list[pathlib.Path] = []
    if (pkgs / "services").is_dir():
        for child in sorted((pkgs / "services").iterdir()):
            if child.is_dir() and not child.name.startswith("_"):
                if (child / "start.sh").is_file():
                    services.append(child)
        # Tolerate trailing "/" in argument resolution above.
    if (pkgs / "pages").is_dir():
        for child in sorted((pkgs / "pages").iterdir()):
            if child.is_dir() and not child.name.startswith("_"):
                pages.append(child)
    return services, pages


def _post_service_register(payload: dict) -> dict:
    try:
        r = httpx.post(f"{BASE_URL}/hub/service/register", json=payload, timeout=15)
    except httpx.HTTPError as exc:
        typer.echo(f"could not reach hub at {BASE_URL}: {exc}", err=True)
        raise typer.Exit(1)
    if r.status_code >= 400:
        typer.echo(f"service register failed ({r.status_code}): {r.text}",
                   err=True)
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


def _spawn_service_local(pkg_dir: pathlib.Path) -> int:
    """Spawn ``start.sh`` for a service with hub URL injected.

    Returns the PID. The service is expected to POST /hub/service/register
    on startup; if it doesn't reconnect within the 10s window the hub
    will SIGTERM this PID and respawn from start.sh itself.
    """
    env = os.environ.copy()
    env["AWM_HUB_URL"] = BASE_URL
    env["AWM_SERVICE_NAME"] = pkg_dir.name
    proc = subprocess.Popen(
        ["bash", str(pkg_dir / "start.sh")],
        cwd=str(pkg_dir),
        env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.pid


@packages_app.command("sync")
def packages_sync(
    workspace: str = typer.Argument(
        ...,
        help="Path to the workspace root containing packages/{services,pages}/.",
    ),
):
    """Discover and register every packages/services/<name> (kind=service)
    and packages/pages/<name> (kind=page) with the hub.

    For each service: spawns ``start.sh`` with AWM_HUB_URL in env (no auth);
    the service then registers itself + opens its control WS.

    For each page: POST /hub/register with the static spec at prefix
    ``/ui/<name>``; this command holds one lease per page until Ctrl-C.
    """
    import asyncio as _asyncio

    ws_root = pathlib.Path(workspace).expanduser().resolve()
    services, pages = _packages_walk(ws_root)
    if not services and not pages:
        typer.echo("no service or page packages found", err=True)
        raise typer.Exit(1)

    leases: list[tuple[str, str]] = []
    spawned_pids: list[int] = []

    for svc_dir in services:
        name = svc_dir.name
        try:
            spawned_pids.append(_spawn_service_local(svc_dir))
            typer.echo(f"spawned service {name} (pid bookkeeping; service "
                       f"self-registers via /hub/service/register)")
        except (OSError, ValueError) as exc:
            typer.echo(f"skip {name}: spawn failed: {exc}", err=True)
            continue

    for page_dir in pages:
        name = page_dir.name
        dist = page_dir / "dist"
        if not dist.is_dir():
            typer.echo(f"skip page {name}: no dist/ — build first", err=True)
            continue
        prefix = _read_prefix_txt(page_dir, f"/ui/{name}")
        try:
            body = _post_page_register(name, prefix, str(dist))
        except typer.Exit:
            continue
        leases.append((name, body["lease_ws_path"]))
        typer.echo(f"registered page {name} → prefix={prefix} "
                   f"id={body['service_id']}")

    if not leases and not spawned_pids:
        typer.echo("nothing registered", err=True)
        raise typer.Exit(1)

    if leases:
        typer.echo(f"holding {len(leases)} page lease(s) (Ctrl-C to evict)…")
        async def _hold_all():
            await _asyncio.gather(*(
                _hold_one_lease(name, path) for name, path in leases
            ))
        try:
            _asyncio.run(_hold_all())
        except KeyboardInterrupt:
            typer.echo("page leases closed")
    else:
        # No page leases but services are running — block on a signal so
        # the user can Ctrl-C to clean up.
        typer.echo(f"services running (pids={spawned_pids}); "
                   "press Ctrl-C to stop")
        try:
            signal.pause()
        except KeyboardInterrupt:
            pass

    # On shutdown, SIGTERM the services we spawned ourselves so they exit
    # cleanly (their start.sh reconnect loop won't help once the hub goes
    # too, but this run is local-CLI only — the hub side keeps going).
    for pid in spawned_pids:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


@packages_app.command("list")
def packages_list():
    """List currently registered packages (services + pages)."""
    r = _local_api("GET", "/hub/services")
    if r.status_code >= 400:
        typer.echo(f"error ({r.status_code}): {r.text}", err=True)
        raise typer.Exit(1)
    _print_json(r)


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


@dev_app.command("start")
def dev_start():
    """Start this worktree's dev sandbox (gateway + bootstrapped services)."""
    _dev_exec("start")


@dev_app.command("stop")
def dev_stop():
    """Stop this worktree's dev sandbox (services + gateway)."""
    _dev_exec("stop")


@dev_app.command("restart")
def dev_restart():
    """Restart this worktree's dev sandbox."""
    _dev_exec("restart")


@dev_app.command("seed")
def dev_seed():
    """(Re)seed the dev sandbox DB without restarting."""
    _dev_exec("seed")


@dev_app.command("status")
def dev_status():
    """Show what this worktree's dev sandbox is running."""
    _dev_exec("status")


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
    target: str = typer.Argument(
        ...,
        help="Explicit path to a service folder (or its run.sh) to overlay — "
             "e.g. awm/services/scopes or "
             "projects/awm/web-ptt/awm/services/scopes. Or 'pages/<name>' to "
             "overlay a built page from ./packages/pages/<name>/dist.",
    ),
    name: Optional[str] = typer.Option(
        None, "--name",
        help="Override the overlay name (defaults to the folder basename).",
    ),
):
    """Overlay a service (or page) onto the running hub.

    Service: execs the folder's ``run.sh`` with ``AWM_SERVICE_OVERLAY=1`` so the
    process registers as a shadow overlay on the existing ``/svc/<name>`` base —
    one process, one identity, its own control WS as the lease. Ctrl-C pops the
    overlay and the base resumes. No second registration, no token, no key file.

    Page ('pages/<name>'): pushes ``./packages/pages/<name>/dist`` as a page
    overlay on ``/ui/<name>`` and holds a lease until Ctrl-C (legacy path).
    """
    # Legacy page-overlay path (relative pages/<name> from a scope worktree).
    if target.startswith("pages/"):
        _dev_shadow_page(target.split("/", 1)[1], name)
        return

    # Service overlay: resolve the explicit path to a run.sh.
    p = pathlib.Path(target).expanduser().resolve()
    if p.is_dir():
        service_dir, run_sh = p, p / "run.sh"
    elif p.name == "run.sh" and p.is_file():
        service_dir, run_sh = p.parent, p
    else:
        typer.echo(f"{target}: expected a service folder containing run.sh "
                   f"(or the run.sh itself); got {p}", err=True)
        raise typer.Exit(1)
    if not run_sh.is_file():
        typer.echo(f"{service_dir}: no run.sh — not a service folder", err=True)
        raise typer.Exit(1)

    base_name = name or service_dir.name      # the service to shadow (its prefix)
    overlay_name = f"{base_name}-shadow"       # unique registry identity
    _require_server()
    # A base must already exist for /svc/<base>, else the overlay register 409s
    # and the adapter retries forever — fail fast with a clear message.
    disc = _local_api("GET", "/hub/services/discovered")
    running = ({s["name"] for s in disc.json().get("services", []) if s["running"]}
               if disc.status_code < 400 else set())
    if base_name not in running:
        typer.echo(f"no running base for /svc/{base_name} — start it first "
                   f"(e.g. `awm services start {base_name}`), then shadow.",
                   err=True)
        raise typer.Exit(1)

    env = os.environ.copy()
    env["AWM_HUB_URL"] = BASE_URL
    env["AWM_SERVICE_NAME"] = overlay_name
    env["AWM_SERVICE_PREFIX"] = f"/svc/{base_name}"
    env["AWM_SERVICE_OVERLAY"] = "1"
    env.setdefault("AWM_ENV", "awm")
    pp = _shadow_dev_pythonpath(service_dir)
    if pp:
        env["DEV_PYTHONPATH"] = pp

    typer.echo(f"shadow {overlay_name} → /svc/{base_name} (overlay); Ctrl-C to pop")
    proc = subprocess.Popen(
        ["bash", str(run_sh)], cwd=str(service_dir), env=env,
        start_new_session=True,
    )
    try:
        proc.wait()
    except KeyboardInterrupt:
        typer.echo("popping overlay…")
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    typer.echo("overlay popped — base traffic resumes")


def _dev_shadow_page(name: str, override_name: Optional[str]) -> None:
    """Legacy: overlay ./packages/pages/<name>/dist on /ui/<name> until Ctrl-C."""
    import asyncio as _asyncio

    pkg_dir = (pathlib.Path.cwd() / "packages" / "pages" / name).expanduser().resolve()
    if not pkg_dir.is_dir():
        typer.echo(f"pages/{name}: {pkg_dir} not a directory", err=True)
        raise typer.Exit(1)
    dist = pkg_dir / "dist"
    if not dist.is_dir():
        typer.echo(f"pages/{name}: no dist/ — build first", err=True)
        raise typer.Exit(1)
    prefix = _read_prefix_txt(pkg_dir, f"/ui/{name}")
    shadow_name = override_name or f"shadow:{name}:{pkg_dir.parent.parent.parent.name}"
    payload = {"name": shadow_name, "prefix": prefix, "page": {"dir": str(dist)}}
    try:
        r = httpx.post(f"{BASE_URL}/hub/shadow/register", json=payload, timeout=15)
    except httpx.HTTPError as exc:
        typer.echo(f"hub unreachable: {exc}", err=True)
        raise typer.Exit(1)
    if r.status_code >= 400:
        typer.echo(f"shadow register failed ({r.status_code}): {r.text}", err=True)
        raise typer.Exit(1)
    body = r.json()
    typer.echo(f"shadow pages/{name} → prefix={prefix} id={body['service_id']}")
    typer.echo("holding shadow lease (Ctrl-C to pop)…")
    try:
        _asyncio.run(_hold_one_lease(f"pages/{name}", body["lease_ws_path"]))
    except KeyboardInterrupt:
        typer.echo("shadow lease closed — base traffic resumes")
    finally:
        for pid in spawned_pids:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
