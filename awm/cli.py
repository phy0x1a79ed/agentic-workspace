"""Typer CLI app with auto-start logic."""

from __future__ import annotations

import os
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
project_app = typer.Typer(help="Project management", no_args_is_help=True)
scope_app = typer.Typer(help="Scope management", no_args_is_help=True)
lock_app = typer.Typer(help="Lock management", no_args_is_help=True)
shared_app = typer.Typer(help="Shared resource edits", no_args_is_help=True)
skill_app = typer.Typer(help="Skills catalog management", no_args_is_help=True)
exposed_app = typer.Typer(help="Network-exposed listener admin", no_args_is_help=True)
peer_app = typer.Typer(help="Federation: manage remote awm peers", no_args_is_help=True)
inbox_app = typer.Typer(help="Inbox: send and read scoped messages", no_args_is_help=True)
room_app = typer.Typer(help="Rooms: multi-participant conversations with agents", no_args_is_help=True)

app.add_typer(project_app, name="project")
app.add_typer(scope_app, name="scope")
app.add_typer(lock_app, name="lock")
app.add_typer(shared_app, name="shared")
app.add_typer(skill_app, name="skill")
app.add_typer(exposed_app, name="exposed")
app.add_typer(peer_app, name="peer")
app.add_typer(inbox_app, name="inbox")
app.add_typer(room_app, name="room")


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


def _exposed_base_and_token() -> tuple[str, str]:
    """Resolve the local exposed URL + bearer token for /rooms calls."""
    from awm import config
    host = os.environ.get("AWM_EXPOSED_HOST", "127.0.0.1")
    if host == "0.0.0.0":
        host = "127.0.0.1"
    port = int(os.environ.get("AWM_EXPOSED_PORT", "7820"))
    base = f"http://{host}:{port}"
    token_env = os.environ.get("AWM_AUTH_TOKEN")
    if token_env:
        return base, token_env.strip()
    candidates = [
        Path(os.environ.get("AWM_AUTH_TOKEN_FILE", str(config.AUTH_TOKEN_FILE))),
        Path.home() / ".awm" / "auth.token",
        config.AUTH_TOKEN_FILE,
    ]
    for token_path in candidates:
        if token_path.exists():
            return base, token_path.read_text().strip()
    raise typer.BadParameter(
        f"auth token not found in any of: {[str(p) for p in candidates]}; "
        f"run `awm exposed init-token`"
    )


def _split_remote(name: str) -> tuple[str, str | None]:
    if "@" in name:
        base, peer = name.rsplit("@", 1)
        return base, peer
    return name, None


def _exposed_api(method: str, path: str, *,
                 peer: str | None = None, **kwargs) -> httpx.Response:
    """Hit a /rooms-style endpoint on the local awm-exposed.

    ``peer`` is reserved for fan-out queries (``--peer all|<id>``) and is
    passed through as a ``?peer`` query param — the local exposed app's
    list/search endpoints handle the tunnel + result merging internally.
    """
    base, token = _exposed_base_and_token()
    headers = kwargs.pop("headers", {}) or {}
    headers["Authorization"] = f"Bearer {token}"
    if peer:
        params = kwargs.pop("params", {}) or {}
        params["peer"] = peer
        kwargs["params"] = params
    r = httpx.request(method, f"{base}{path}", headers=headers, timeout=30, **kwargs)
    return r


def _peer_direct_api(method: str, peer_id: str, path: str, **kwargs) -> httpx.Response:
    """Hit a peer's awm-exposed endpoint *directly* through an SSH tunnel.

    Use this for ``room@peer`` semantics where we want the remote peer to
    own the operation (e.g. ``awm room post name@xaw`` should make the
    post land on xaw's transcript, not be forwarded via our local rooms
    service)."""
    from awm.services.network import ssh_tunnel
    from awm.services.network import peers as peer_svc
    try:
        tun = ssh_tunnel.acquire_tunnel(peer_id)
    except ssh_tunnel.TunnelError as exc:
        raise typer.BadParameter(f"could not tunnel to {peer_id}: {exc}")
    try:
        token = peer_svc.load_peer_token(peer_id)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        raise typer.BadParameter(str(exc))
    headers = kwargs.pop("headers", {}) or {}
    headers["Authorization"] = f"Bearer {token}"
    from awm.services.network import peers as _peers
    local = _peers.get_local_identity()
    if local:
        headers["X-Awm-From"] = local["peer_id"]
    r = httpx.request(method, f"{tun.local_url}{path}", headers=headers, timeout=30, **kwargs)
    return r


def _api_for_room(method: str, name: str, suffix: str, **kwargs) -> httpx.Response:
    """Route a room CLI op to either local or via-tunnel based on @peer."""
    base, peer = _split_remote(name)
    if peer:
        return _peer_direct_api(method, peer, f"/rooms/{base}{suffix}", **kwargs)
    return _exposed_api(method, f"/rooms/{base}{suffix}", **kwargs)


# ---------------------------------------------------------------------------
# Top-level commands
# ---------------------------------------------------------------------------

@app.command()
def init():
    """Bootstrap workspace and initialize the AWM database."""
    from awm.db import init_db
    AWM_DIR.mkdir(parents=True, exist_ok=True)

    # Ensure workspace directories exist
    for d in ["data/reference", "projects", "main"]:
        (WORKSPACE_ROOT / d).mkdir(parents=True, exist_ok=True)

    init_db()
    typer.echo(f"Initialized AWM at {AWM_DIR}")
    typer.echo(f"Database: {AWM_DIR / 'state.db'}")
    typer.echo(f"Workspace: {WORKSPACE_ROOT}")


@app.command()
def serve():
    """Run the AWM server in the foreground."""
    from awm.server import run_server
    run_server()


@app.command("serve-exposed")
def serve_exposed():
    """Run the network-exposed HTTPS listener (separate from local core).

    Reads bind/port from AWM_EXPOSED_HOST / AWM_EXPOSED_PORT (default
    127.0.0.1:7820). Enables TLS if AWM_TLS_CERT and AWM_TLS_KEY are set.
    Requires an auth token in $AWM_AUTH_TOKEN or AUTH_TOKEN_FILE
    (default ~/.awm/auth.token) — generate one with ``awm exposed init-token``.
    """
    from awm.exposed import run_exposed_server
    run_exposed_server()


@exposed_app.command("init-token")
def exposed_init_token(
    force: bool = typer.Option(False, "--force", help="Overwrite existing token"),
):
    """Generate a random bearer token and write it to AUTH_TOKEN_FILE.

    Prints the token once. The file is chmod 600. Rotate later by re-running
    with --force; the listener picks up the change on the next request.
    """
    import secrets
    from awm import config as _config

    path = _config.AUTH_TOKEN_FILE
    if path.exists() and not force:
        typer.echo(
            f"Token file already exists at {path}. Use --force to overwrite.",
            err=True,
        )
        raise typer.Exit(1)

    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    typer.echo(f"Token written to {path}")
    typer.echo(f"Token: {token}")
    typer.echo("Use as: Authorization: Bearer <token>")


@exposed_app.command("status")
def exposed_status():
    """Check the exposed listener via /status (no auth required for ping)."""
    from awm import config as _config
    import json as _json

    # Try TLS first if cert configured, else plain.
    scheme = "https" if _config.TLS_CERT else "http"
    url = f"{scheme}://{_config.EXPOSED_HOST}:{_config.EXPOSED_PORT}/status"
    token_env = os.environ.get(_config.AUTH_TOKEN_ENV)
    token_file = _config.AUTH_TOKEN_FILE
    token = None
    if token_env:
        token = token_env.strip()
    elif token_file.exists():
        token = token_file.read_text().strip()

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = httpx.get(url, headers=headers, timeout=5, verify=False)
    except httpx.HTTPError as exc:
        typer.echo(f"Could not reach exposed listener at {url}: {exc}", err=True)
        raise typer.Exit(1)
    if r.status_code >= 400:
        typer.echo(f"Error ({r.status_code}): {r.text}", err=True)
        raise typer.Exit(1)
    typer.echo(_json.dumps(r.json(), indent=2))


@app.command()
def status():
    """Show server health + active locks + scopes summary."""
    r = _api("GET", "/status")
    _print_json(r)


# ---------------------------------------------------------------------------
# Peer (federation)
# ---------------------------------------------------------------------------

@peer_app.command("init")
def peer_init(
    peer_id: str = typer.Argument(..., help="Stable identifier for this awm instance"),
    advertise_url: str = typer.Option(None, "--advertise-url",
                                       help="Optional cosmetic URL (transport is SSH-tunneled)"),
    overwrite: bool = typer.Option(False, "--overwrite",
                                    help="Replace an existing identity file"),
):
    """Set the local peer identity (writes config.PEER_FILE)."""
    from awm.services.network import peers as peer_svc
    try:
        data = peer_svc.set_local_identity(peer_id, advertise_url, overwrite=overwrite)
    except peer_svc.LocalIdentityError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2)
    import json as _json
    typer.echo(_json.dumps(data, indent=2))


@peer_app.command("add")
def peer_add(
    peer_id: str = typer.Argument(..., help="Remote peer's identifier"),
    ssh_alias: str = typer.Option("", "--ssh-alias",
                                  help="SSH host alias (omit/empty = loopback mode: peer already reachable on 127.0.0.1:--remote-port via an out-of-band reverse forward)"),
    remote_port: int = typer.Option(7820, "--remote-port",
                                    help="Port to reach the peer's awm-exposed on (default 7820); in loopback mode this is the local 127.0.0.1 port"),
    token_file: str = typer.Option(..., "--token-file",
                                    help="Path to the bearer token file (copied to canonical location)"),
    friendly_name: str = typer.Option(None, "--name", help="Optional friendly name"),
):
    """Register a remote awm peer reachable via an SSH alias.

    The local peer opens a port-forwarded SSH ControlMaster on first use;
    all federation HTTP and WebSocket traffic runs through it.
    """
    from awm.services.network import peers as peer_svc
    try:
        peer_svc.install_peer_token(peer_id, token_file)
        entry = peer_svc.add_peer(
            peer_id, ssh_alias,
            remote_port=remote_port,
            friendly_name=friendly_name,
        )
    except (ValueError, FileNotFoundError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2)
    import json as _json
    typer.echo(_json.dumps(entry, indent=2))


@peer_app.command("list")
def peer_list():
    """List registered remote peers."""
    from awm.services.network import peers as peer_svc
    import json as _json
    typer.echo(_json.dumps(peer_svc.list_peers(), indent=2))


@peer_app.command("remove")
def peer_remove(peer_id: str = typer.Argument(...)):
    """Remove a registered peer."""
    from awm.services.network import peers as peer_svc
    if not peer_svc.remove_peer(peer_id):
        typer.echo(f"no such peer: {peer_id}", err=True)
        raise typer.Exit(1)
    typer.echo(f"removed peer: {peer_id}")


@peer_app.command("ping")
def peer_ping(
    peer_id: str = typer.Argument(...),
    timeout: float = typer.Option(5.0, "--timeout"),
):
    """Probe a peer's /peer endpoint, verify identity, and update last_seen."""
    from awm.services.network import peers as peer_svc
    import json as _json
    result = peer_svc.ping_peer(peer_id, timeout=timeout)
    typer.echo(_json.dumps(dict(result), indent=2))
    if not result.get("ok"):
        raise typer.Exit(1)


@inbox_app.command("send")
def inbox_send(
    scope: str = typer.Argument(..., help="Target scope, e.g. 'scope:foo/bar' or 'scope:foo/bar@crux'"),
    subject: str = typer.Option(..., "--subject"),
    body: str = typer.Option(..., "--body"),
    sender: str = typer.Option("operator", "--sender"),
    msg_type: str = typer.Option("notification", "--type",
                                  help="One of: scope_assignment, reflection, status_update, notification, plan"),
    metadata: str = typer.Option(None, "--metadata", help="Optional JSON-encoded metadata"),
):
    """Send a message. With ``@<peer-id>`` suffix, routes to the remote peer."""
    payload = {
        "scope": scope, "sender": sender, "msg_type": msg_type,
        "subject": subject, "body": body, "metadata": metadata,
    }
    r = _api("POST", "/inbox", json=payload)
    _print_json(r)


@inbox_app.command("fetch")
def inbox_fetch(
    scope: str = typer.Argument(..., help="Scope to fetch"),
    mark_read: bool = typer.Option(False, "--mark-read"),
    limit: int = typer.Option(50, "--limit"),
):
    """Fetch full messages from a scope (local only — no @peer routing)."""
    params = {"scope": scope, "mark_read": str(mark_read).lower(), "limit": limit}
    r = _api("GET", "/messages/fetch", params=params)
    _print_json(r)


@inbox_app.command("search")
def inbox_search(
    scope: str = typer.Option(None, "--scope"),
    status: str = typer.Option(None, "--status"),
    msg_type: str = typer.Option(None, "--type"),
    query: str = typer.Option(None, "--query"),
    limit: int = typer.Option(50, "--limit"),
):
    """Search message previews (local only — no @peer routing)."""
    params = {}
    for k, v in (("scope", scope), ("status", status), ("msg_type", msg_type),
                 ("query", query), ("limit", limit)):
        if v is not None:
            params[k] = v
    r = _api("GET", "/messages", params=params)
    _print_json(r)


@room_app.command("create")
def room_create(
    topic: str = typer.Option(None, "--topic", help="Optional human-readable topic"),
    scope: list[str] = typer.Option(
        None, "--scope",
        help="Scope to enroll (project/scope[@peer]); repeatable",
    ),
    prompt: list[str] = typer.Option(
        None, "--prompt",
        help="Initial prompt per scope, as SCOPE=text; repeatable. Bare text "
             "(no '=') applies to the first --scope.",
    ),
    close_on_exit: bool = typer.Option(False, "--close-on-exit"),
):
    """Create a room and (optionally) spawn agents on the given scopes."""
    prompts: dict[str, str] = {}
    if prompt:
        for p in prompt:
            if "=" in p:
                key, _, val = p.partition("=")
                prompts[key.strip()] = val
            elif scope:
                prompts[scope[0]] = p
    payload = {
        "topic": topic, "scopes": scope or [], "prompts": prompts,
        "close_on_exit": close_on_exit,
    }
    r = _exposed_api("POST", "/rooms", json=payload)
    _print_json(r)


@room_app.command("list")
def room_list(
    peer: str = typer.Option(None, "--peer", help="all|<peer-id> for fan-out"),
    status: str = typer.Option("active", "--status"),
    participating_scope: str = typer.Option(None, "--participating-scope"),
):
    """List rooms (locally or across peers)."""
    params = {"status": status}
    if participating_scope:
        params["participating_scope"] = participating_scope
    if peer:
        params["peer"] = peer
    r = _exposed_api("GET", "/rooms", params=params)
    _print_json(r)


@room_app.command("get")
def room_get(name: str = typer.Argument(...)):
    """Show room details, participants, and recent transcript."""
    r = _api_for_room("GET", name, "")
    _print_json(r)


@room_app.command("history")
def room_history(
    name: str = typer.Argument(...),
    before_ts: str = typer.Option(None, "--before"),
    limit_chars: int = typer.Option(4096, "--limit-chars"),
):
    """Get a longer slice of the room's transcript."""
    params = {"limit_chars": limit_chars}
    if before_ts:
        params["before_ts"] = before_ts
    r = _api_for_room("GET", name, "/history", params=params)
    _print_json(r)


@room_app.command("search")
def room_search(
    query: str = typer.Argument(...),
    peer: str = typer.Option(None, "--peer", help="all|<peer-id> for fan-out"),
    limit: int = typer.Option(20, "--limit"),
):
    """Search rooms by topic / id / transcript content."""
    params = {"q": query, "limit": limit}
    if peer:
        params["peer"] = peer
    r = _exposed_api("GET", "/rooms/search", params=params)
    _print_json(r)


@room_app.command("post")
def room_post(
    name: str = typer.Argument(...),
    text: str = typer.Argument(...),
    to_scope: str = typer.Option(None, "--to", help="Direct-address a scope"),
):
    """Post a message to a room (as the local operator)."""
    r = _api_for_room("POST", name, "/posts", json={"body": text, "to": to_scope})
    _print_json(r)


@room_app.command("invite")
def room_invite(
    name: str = typer.Argument(...),
    scope: str = typer.Option(..., "--scope"),
    prompt: str = typer.Option(None, "--prompt"),
):
    """Invite a scope (agent) into a room."""
    r = _api_for_room(
        "POST", name, "/invite",
        json={"scope": scope, "prompt": prompt},
    )
    _print_json(r)


@room_app.command("remove")
def room_remove(
    name: str = typer.Argument(...),
    scope: str = typer.Option(..., "--scope"),
):
    """Remove a scope from a room (doesn't kill the agent process)."""
    r = _api_for_room("POST", name, "/remove", json={"scope": scope})
    _print_json(r)


@room_app.command("close")
def room_close(
    name: str = typer.Argument(...),
    kill_agents: bool = typer.Option(False, "--kill-agents"),
):
    """Close a room (optionally SIGTERM all participant agents)."""
    r = _api_for_room(
        "POST", name, "/close", json={"kill_agents": kill_agents},
    )
    _print_json(r)


@room_app.command("archive")
def room_archive(name: str = typer.Argument(...)):
    """Soft-archive a room. Refused (409) if active scope participants remain."""
    r = _api_for_room("POST", name, "/archive")
    _print_json(r)


@room_app.command("agents")
def room_agents(name: str = typer.Argument(...)):
    """List room participants with live agent state (PID, status)."""
    r = _api_for_room("GET", name, "/agents")
    _print_json(r)


@room_app.command("join")
def room_join(
    name: str = typer.Argument(...),
    quiet: bool = typer.Option(False, "--quiet",
                                help="Skip the interactive prompt; just stream events"),
):
    """Attach to a room's WebSocket — stream events to stdout, accept
    stdin lines as posts. Terminates on Ctrl-D / Ctrl-C."""
    import asyncio
    import json as _json
    import threading
    import websockets as _ws

    base_name, peer = _split_remote(name)
    if peer:
        from awm.services.network import ssh_tunnel
        from awm.services.network import peers as peer_svc
        try:
            tun = ssh_tunnel.acquire_tunnel(peer)
        except ssh_tunnel.TunnelError as exc:
            raise typer.BadParameter(f"could not tunnel to {peer}: {exc}")
        token = peer_svc.load_peer_token(peer)
        base_url = tun.local_url
    else:
        base_url, token = _exposed_base_and_token()
    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
    uri = f"{ws_url}/rooms/{base_name}/attach"

    async def runner():
        async with _ws.connect(uri, subprotocols=[f"bearer.{token}"]) as ws:
            stop = asyncio.Event()

            async def reader():
                try:
                    async for raw in ws:
                        try:
                            ev = _json.loads(raw)
                        except _json.JSONDecodeError:
                            continue
                        t = ev.get("type")
                        if t == "history":
                            for p in ev.get("posts", []):
                                typer.echo(f"[{p['ts']}] {p['author']}: {p['body']}")
                        elif t == "post":
                            p = ev["post"]
                            typer.echo(f"[{p['ts']}] {p['author']}: {p['body']}")
                        elif t == "participant_joined":
                            typer.echo(f"-- {ev['participant']['kind']}:"
                                       f"{ev['participant']['identifier']} joined --")
                        elif t == "participant_left":
                            typer.echo(f"-- {ev['participant']['kind']}:"
                                       f"{ev['participant']['identifier']} left --")
                        elif t == "room_closed":
                            typer.echo(f"-- room closed @ {ev['ts']} --")
                            stop.set()
                            return
                        elif t == "lagged":
                            typer.echo("-- lagged; closing --", err=True)
                            stop.set()
                            return
                        elif t == "error":
                            typer.echo(f"-- error: {ev.get('message')}", err=True)
                except _ws.ConnectionClosed:
                    pass
                stop.set()

            async def writer():
                loop = asyncio.get_event_loop()
                if quiet:
                    await stop.wait()
                    return
                while not stop.is_set():
                    try:
                        line = await loop.run_in_executor(None, sys.stdin.readline)
                    except KeyboardInterrupt:
                        break
                    if not line:  # EOF
                        break
                    text = line.rstrip("\n")
                    if not text:
                        continue
                    try:
                        await ws.send(_json.dumps({"type": "post", "body": text}))
                    except _ws.ConnectionClosed:
                        break
                stop.set()

            await asyncio.gather(reader(), writer())

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        pass


@room_app.command("one-off")
def room_one_off(
    scope: str = typer.Option(..., "--scope"),
    prompt: str = typer.Option(..., "--prompt"),
    topic: str = typer.Option(None, "--topic"),
):
    """Sugar for ``create --scope ... --prompt ... --close-on-exit``."""
    payload = {
        "topic": topic, "scopes": [scope], "prompts": {scope: prompt},
        "close_on_exit": True,
    }
    r = _exposed_api("POST", "/rooms", json=payload)
    _print_json(r)


@peer_app.command("whoami")
def peer_whoami():
    """Show this instance's local peer identity (or report 'unset')."""
    from awm.services.network import peers as peer_svc
    import json as _json
    try:
        ident = peer_svc.get_local_identity()
    except peer_svc.LocalIdentityError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)
    if ident is None:
        typer.echo("local peer identity is unset — run `awm peer init`", err=True)
        raise typer.Exit(1)
    typer.echo(_json.dumps(ident, indent=2))


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
                       "(see projects/awm/release/deploy/awm.service).", err=True)
            raise typer.Exit(result.returncode)
        typer.echo("awm core restarted (direct systemctl fallback). MCP clients reconnect on next tool call.")


# ---------------------------------------------------------------------------
# Project commands
# ---------------------------------------------------------------------------

@project_app.command("create")
def project_create(
    name: str = typer.Argument(..., help="Project name"),
    clone: Optional[str] = typer.Option(None, "--clone", help="Clone from URL"),
    fork: Optional[str] = typer.Option(None, "--fork", help="Fork from URL"),
):
    """Create a new project."""
    payload = {"name": name}
    if clone:
        payload["clone_url"] = clone
    elif fork:
        payload["fork_url"] = fork
    r = _api("POST", "/projects", json=payload)
    _print_json(r)


@project_app.command("list")
def project_list():
    """List projects with per-status scope counts."""
    r = _api("GET", "/projects")
    _print_json(r)


# ---------------------------------------------------------------------------
# Scope commands
# ---------------------------------------------------------------------------

@scope_app.command("create")
def scope_create(
    project: str = typer.Argument(..., help="Project name"),
    scope: str = typer.Argument(..., help="Scope name"),
    from_branch: Optional[str] = typer.Option(None, "--from", help="Base branch"),
    context: Optional[str] = typer.Option(None, "--context", help="Seed context text for AGENTS.md"),
    context_file: Optional[Path] = typer.Option(None, "--context-file", help="Read context from file"),
):
    """Create a scope worktree."""
    payload = {"project": project, "scope": scope}
    if from_branch:
        payload["from_branch"] = from_branch
    if context_file:
        payload["context"] = context_file.read_text(encoding="utf-8")
    elif context:
        payload["context"] = context
    r = _api("POST", "/scopes", json=payload)
    _print_json(r)


@scope_app.command("complete")
def scope_complete(
    project: str = typer.Argument(..., help="Project name"),
    scope: str = typer.Argument(..., help="Scope name"),
    merge: bool = typer.Option(False, "--merge", help="Merge feature branch into main"),
    cleanup: bool = typer.Option(False, "--cleanup", help="Remove worktree and branch after completion"),
):
    """Complete a scope."""
    r = _api("PATCH", f"/scopes/{project}/{scope}", json={"action": "complete", "merge": merge, "cleanup": cleanup})
    _print_json(r)


@scope_app.command("delete")
def scope_delete(
    project: str = typer.Argument(..., help="Project name"),
    scope: str = typer.Argument(..., help="Scope name"),
    force: bool = typer.Option(False, "--force", help="Skip confirmation"),
):
    """Delete a scope (remove worktree, branch, mark as deleted)."""
    if not force:
        typer.confirm(f"Delete scope '{scope}' in project '{project}'? This removes the worktree and branch.", abort=True)
    r = _api("DELETE", f"/scopes/{project}/{scope}")
    _print_json(r)


@scope_app.command("list")
def scope_list(
    status: Optional[str] = typer.Option(None, "--status", help="Filter by status (active/completed/deleted/all)"),
    project: Optional[str] = typer.Option(None, "--project", help="Filter by project"),
):
    """List scopes."""
    params = {}
    if status:
        params["status"] = status
    if project:
        params["project"] = project
    r = _api("GET", "/scopes", params=params)

    data = r.json()
    if r.status_code >= 400:
        typer.echo(f"Error: {data}", err=True)
        raise typer.Exit(1)

    scopes = data["scopes"]
    if not scopes:
        typer.echo("(no scopes found)")
        return

    # Table output
    typer.echo(f"{'PROJECT':<20} {'SCOPE':<25} {'STATUS':<12} {'BRANCH':<30}")
    typer.echo(f"{'-------':<20} {'-----':<25} {'------':<12} {'------':<30}")
    for s in scopes:
        typer.echo(f"{s['project']:<20} {s['scope']:<25} {s['status']:<12} {s['branch']:<30}")
    typer.echo(f"\nTotal: {data['total']} scope(s)")


# ---------------------------------------------------------------------------
# Lock commands
# ---------------------------------------------------------------------------

@lock_app.command("acquire")
def lock_acquire(
    path: str = typer.Argument(..., help="Resource path to lock"),
    holder: str = typer.Option(..., "--holder", help="Agent/holder identifier"),
    lock_type: str = typer.Option("exclusive", "--type", help="Lock type: exclusive or shared"),
):
    """Acquire a lock on a resource."""
    payload = {
        "resource_path": path,
        "holder_id": holder,
        "lock_type": lock_type,
        "holder_pid": os.getpid(),
    }
    r = _api("POST", "/locks", json=payload)
    _print_json(r)


@lock_app.command("release")
def lock_release(
    path: str = typer.Argument(..., help="Resource path to unlock"),
    holder: str = typer.Option(..., "--holder", help="Agent/holder identifier"),
):
    """Release a lock."""
    r = _api("DELETE", "/locks", params={"path": path, "holder": holder})
    _print_json(r)


@lock_app.command("list")
def lock_list(
    holder: Optional[str] = typer.Option(None, "--holder", help="Filter by holder"),
    path: Optional[str] = typer.Option(None, "--path", help="Filter by path"),
):
    """List active locks."""
    params = {}
    if holder:
        params["holder"] = holder
    if path:
        params["path"] = path
    r = _api("GET", "/locks", params=params)

    data = r.json()
    if r.status_code >= 400:
        typer.echo(f"Error: {data}", err=True)
        raise typer.Exit(1)

    locks = data["locks"]
    if not locks:
        typer.echo("(no active locks)")
        return

    typer.echo(f"{'RESOURCE':<40} {'HOLDER':<20} {'TYPE':<12} {'ACQUIRED':<25}")
    typer.echo(f"{'--------':<40} {'------':<20} {'----':<12} {'--------':<25}")
    for lk in locks:
        typer.echo(f"{lk['resource_path']:<40} {lk['holder_id']:<20} {lk['lock_type']:<12} {lk['acquired_at']:<25}")
    typer.echo(f"\nTotal: {data['total']} lock(s)")


@lock_app.command("reap")
def lock_reap():
    """Force stale lock cleanup."""
    r = _api("POST", "/locks/reap")
    data = r.json()
    typer.echo(f"Reaped {data['reaped']} stale lock(s)")


# ---------------------------------------------------------------------------
# Shared resource commands
# ---------------------------------------------------------------------------

@shared_app.command("edit")
def shared_edit(
    name: str = typer.Option(..., "--name", help="Name for this shared edit"),
    created_by: str = typer.Option("unknown", "--by", help="Who is creating this edit"),
):
    """Start a shared resource edit (creates worktree)."""
    r = _api("POST", "/shared", json={"name": name, "created_by": created_by})
    _print_json(r)


@shared_app.command("merge")
def shared_merge(
    name: str = typer.Option(..., "--name", help="Name of the shared edit to merge"),
):
    """Merge a shared resource edit back."""
    r = _api("POST", f"/shared/{name}/merge")
    _print_json(r)


@shared_app.command("list")
def shared_list(
    status: Optional[str] = typer.Option(None, "--status", help="Filter by status"),
):
    """List shared edits."""
    params = {}
    if status:
        params["status"] = status
    r = _api("GET", "/shared", params=params)

    data = r.json()
    if r.status_code >= 400:
        typer.echo(f"Error: {data}", err=True)
        raise typer.Exit(1)

    edits = data["edits"]
    if not edits:
        typer.echo("(no shared edits)")
        return

    typer.echo(f"{'NAME':<30} {'STATUS':<12} {'BRANCH':<30} {'CREATED BY':<20}")
    typer.echo(f"{'----':<30} {'------':<12} {'------':<30} {'----------':<20}")
    for e in edits:
        typer.echo(f"{e['name']:<30} {e['status']:<12} {e['branch']:<30} {e['created_by']:<20}")
    typer.echo(f"\nTotal: {data['total']} edit(s)")


# ---------------------------------------------------------------------------
# Skill commands
# ---------------------------------------------------------------------------

def _resolve_peer_set(peer_flag: str | None) -> list[str] | None:
    """Translate a --peer flag into a list of peer_ids, or None for local-only.

    ``--peer all`` returns every registered peer; ``--peer <id>`` returns
    just that peer (verifies it exists); absent flag returns None.
    """
    if not peer_flag:
        return None
    from awm.services.network import peers as _peers
    if peer_flag == "all":
        return [p["peer_id"] for p in _peers.list_peers()]
    if _peers.get_peer(peer_flag) is None:
        typer.echo(f"error: unknown peer '{peer_flag}'", err=True)
        raise typer.Exit(2)
    return [peer_flag]


@skill_app.command("list")
def skill_list(
    type: Optional[str] = typer.Option(None, "--type", help="Filter by type (sop, tool, template)"),
    tags: Optional[str] = typer.Option(None, "--tags", help="Comma-separated tags to filter by"),
    peer: Optional[str] = typer.Option(None, "--peer",
                                        help="Federate: 'all' or a peer-id. Excludes local."),
):
    """List all skills in the catalog. With --peer, fetches from remote peers."""
    params = {}
    if type:
        params["type"] = type
    if tags:
        params["tags"] = tags

    peer_ids = _resolve_peer_set(peer)
    if peer_ids is not None:
        from awm.services.network import federation as _fed
        data = _fed.fan_out_get(peer_ids, "/skills", params, result_key="skills")
    else:
        r = _api("GET", "/skills", params=params)
        if r.status_code >= 400:
            typer.echo(f"Error: {r.text}", err=True)
            raise typer.Exit(1)
        data = r.json()

    skills = data["skills"]
    if not skills:
        typer.echo("(no skills found)")
        if data.get("degraded"):
            typer.echo(f"degraded peers: {data['degraded']}", err=True)
        return

    typer.echo(f"{'NAME':<25} {'TYPE':<10} {'TAGS':<30} {'FILE':<35} {'PEER':<10}")
    typer.echo(f"{'----':<25} {'----':<10} {'----':<30} {'----':<35} {'----':<10}")
    for s in skills:
        tags_str = ", ".join(s.get("tags", []))
        origin = s.get("origin_peer_id", "local")
        typer.echo(f"{s['name']:<25} {s['type']:<10} {tags_str:<30} {s['file_path']:<35} {origin:<10}")
    typer.echo(f"\nTotal: {data['total']} skill(s)")
    if data.get("degraded"):
        typer.echo(f"Degraded peers: {data['degraded']}", err=True)


@skill_app.command("get")
def skill_get(
    path: str = typer.Argument(..., help="Relative path to skill (e.g. tools/git.md)"),
):
    """Read a skill file."""
    r = _api("GET", f"/skills/{path}")
    if r.status_code >= 400:
        typer.echo(f"Error ({r.status_code}): {r.text}", err=True)
        raise typer.Exit(1)
    data = r.json()
    typer.echo(data["content"])


@skill_app.command("search")
def skill_search(
    query: str = typer.Argument(..., help="Search query"),
    peer: Optional[str] = typer.Option(None, "--peer",
                                        help="Federate: 'all' or a peer-id. Excludes local."),
):
    """Search skills by name, tags, description, or content. With --peer,
    fan out to remote peers and tag each result with its origin."""
    peer_ids = _resolve_peer_set(peer)
    if peer_ids is not None:
        from awm.services.network import federation as _fed
        data = _fed.fan_out_get(peer_ids, "/skills/search", {"q": query}, result_key="skills")
    else:
        r = _api("GET", "/skills/search", params={"q": query})
        if r.status_code >= 400:
            typer.echo(f"Error: {r.text}", err=True)
            raise typer.Exit(1)
        data = r.json()

    skills = data["skills"]
    if not skills:
        typer.echo("(no matching skills)")
        if data.get("degraded"):
            typer.echo(f"degraded peers: {data['degraded']}", err=True)
        return

    typer.echo(f"{'NAME':<25} {'TYPE':<10} {'FILE':<35} {'PEER':<10}")
    typer.echo(f"{'----':<25} {'----':<10} {'----':<35} {'----':<10}")
    for s in skills:
        origin = s.get("origin_peer_id", "local")
        typer.echo(f"{s['name']:<25} {s['type']:<10} {s['file_path']:<35} {origin:<10}")
    typer.echo(f"\nTotal: {data['total']} match(es)")
    if data.get("degraded"):
        typer.echo(f"Degraded peers: {data['degraded']}", err=True)


# ---------------------------------------------------------------------------
# Session commands — registered via registry + migrate-experiences
# ---------------------------------------------------------------------------

from awm.operations.sessions import SESSION_OPERATIONS
from awm.registry import register_cli_commands

_session_groups = register_cli_commands(app, SESSION_OPERATIONS, _api)
_session_app = _session_groups["session"]


@_session_app.command("migrate-experiences")
def session_migrate_experiences():
    """Migrate experiences.md files into the database."""
    from awm.db import init_db

    init_db()
    from awm.services import sessions

    stats = sessions.migrate_experiences()
    typer.echo(f"Files processed: {stats['files_processed']}")
    typer.echo(f"Imported: {stats['imported']}")
    typer.echo(f"Skipped (already in DB): {stats['skipped']}")
