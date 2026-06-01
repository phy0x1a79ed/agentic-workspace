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
project_app = typer.Typer(help="Project management", no_args_is_help=True)
scope_app = typer.Typer(help="Scope management", no_args_is_help=True)
lock_app = typer.Typer(help="Lock management", no_args_is_help=True)
shared_app = typer.Typer(help="Shared resource edits", no_args_is_help=True)
skill_app = typer.Typer(help="Skills catalog management", no_args_is_help=True)
exposed_app = typer.Typer(help="Network-exposed listener admin", no_args_is_help=True)
peer_app = typer.Typer(help="Federation: manage remote awm peers", no_args_is_help=True)
inbox_app = typer.Typer(help="Inbox: send and read scoped messages", no_args_is_help=True)
room_app = typer.Typer(help="Rooms: multi-participant conversations with agents", no_args_is_help=True)

discord_app = typer.Typer(help="Discord bot operator whitelist", no_args_is_help=True)
hub_app = typer.Typer(help="Service hub: register + lease external services", no_args_is_help=True)
stripe_app = typer.Typer(help="Vertical stripes: register packages/* with the hub (kind=stripe)", no_args_is_help=True)

context_app = typer.Typer(help="Scope context: emit .awm/context.md for harness SessionStart hooks", no_args_is_help=True)

app.add_typer(project_app, name="project")
app.add_typer(scope_app, name="scope")
app.add_typer(lock_app, name="lock")
app.add_typer(shared_app, name="shared")
app.add_typer(skill_app, name="skill")
app.add_typer(exposed_app, name="exposed")
app.add_typer(peer_app, name="peer")
app.add_typer(inbox_app, name="inbox")
app.add_typer(room_app, name="room")
app.add_typer(discord_app, name="discord")
app.add_typer(context_app, name="context")
app.add_typer(hub_app, name="hub")
app.add_typer(stripe_app, name="stripe")


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
    """Resolve the local exposed URL + bearer token for /rooms calls.

    Returns ``(base_url, token)``. Resolution order:

      1. ``AWM_EXPOSED_HOST`` / ``AWM_EXPOSED_PORT`` env vars (dev escape
         hatch).
      2. ``$AWM_DIR/exposed.json`` written by ``serve-exposed`` lifespan
         (the source-of-truth discovery file — eliminates the config
         drift in inbox bugs #160/#161/#166).
      3. Hardcoded fallback ``https://127.0.0.1:7820`` if neither is
         present (e.g. before the daemon has ever run).

    Operators never see a ``--token`` flag — the auth/TLS ritual lives in
    :mod:`awm.services.auth`.
    """
    from awm.services import auth as _auth
    try:
        token = _auth.local_token(generate_if_missing=False)
    except _auth.TokenMissing as exc:
        raise typer.BadParameter(str(exc))

    env_host = os.environ.get("AWM_EXPOSED_HOST")
    env_port = os.environ.get("AWM_EXPOSED_PORT")
    if env_host or env_port:
        host = env_host or "127.0.0.1"
        if host == "0.0.0.0":
            host = "127.0.0.1"
        port = int(env_port) if env_port else 7820
        return f"https://{host}:{port}", token

    discovery_path = AWM_DIR / "exposed.json"
    if discovery_path.exists():
        try:
            import json as _json
            data = _json.loads(discovery_path.read_text())
            scheme = data.get("scheme", "https")
            host = data.get("host") or "127.0.0.1"
            if host == "0.0.0.0":
                host = "127.0.0.1"
            port = int(data.get("port") or 7820)
            return f"{scheme}://{host}:{port}", token
        except (OSError, ValueError):
            pass

    return "https://127.0.0.1:7820", token


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
    # Self-signed TLS on loopback — bearer is the trust boundary.
    r = httpx.request(
        method, f"{base}{path}", headers=headers, timeout=30, verify=False,
        **kwargs,
    )
    return r


def _peer_direct_api(method: str, peer_id: str, path: str, **kwargs) -> httpx.Response:
    """Hit a peer's HTTPS endpoint directly via the peer-client resolver.

    Honors the peer's ``endpoints`` list (direct → SSH fallback) configured
    via ``awm peer add --endpoint ...``. The bearer is the peer-token
    installed under ``$AWM_DIR/peers/<peer_id>.token``; ``X-Awm-From`` is
    our local peer id so the remote tags the post correctly.

    Use this for ``room@peer`` semantics where the remote peer owns the
    operation (``awm room post name@xps`` lands on xps's transcript).
    """
    from awm.services.network import federation, peers as _peers
    try:
        base_url, token = federation._resolve(peer_id)
    except federation.FederationError as exc:
        raise typer.BadParameter(str(exc))
    headers = kwargs.pop("headers", {}) or {}
    headers["Authorization"] = f"Bearer {token}"
    local = _peers.get_local_identity()
    if local:
        headers["X-Awm-From"] = local["peer_id"]
    r = httpx.request(
        method, f"{base_url}{path}", headers=headers, timeout=30,
        verify=False, **kwargs,
    )
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
    """Check the exposed listener via /status.

    Reads ``$AWM_DIR/exposed.json`` (written by the live daemon) for
    scheme/host/port so we don't guess. Falls back to env vars +
    hardcoded defaults if the discovery file is missing.
    """
    import json as _json
    import ssl as _ssl
    base, token = _exposed_base_and_token()
    url = f"{base}/status"
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    def _try(target_url: str):
        return httpx.get(target_url, headers=headers, timeout=5, verify=False)

    try:
        r = _try(url)
    except httpx.HTTPError as exc:
        # If exposed.json said HTTPS but the listener is actually a hand-rolled
        # plain-HTTP one, the TLS handshake will surface as SSLError wrapped in
        # an httpx.ConnectError. Try HTTP once before giving up.
        is_ssl = isinstance(exc.__cause__, _ssl.SSLError) or isinstance(exc, _ssl.SSLError)
        if base.startswith("https://") and is_ssl:
            fallback = "http://" + base[len("https://"):]
            try:
                r = _try(f"{fallback}/status")
            except httpx.HTTPError as exc2:
                typer.echo(
                    f"Could not reach exposed listener at {url} "
                    f"(also tried {fallback}/status): {exc2}",
                    err=True,
                )
                raise typer.Exit(1)
        else:
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
    token_file: Optional[str] = typer.Option(
        None, "--token-file",
        help="Path to the bearer token file (copied to canonical location). "
             "Mutually exclusive with --bootstrap-via-ssh.",
    ),
    bootstrap_via_ssh: bool = typer.Option(
        False, "--bootstrap-via-ssh",
        help="Fetch the peer's bearer over SSH using --ssh-alias "
             "(reads ~/.awm/auth.token on the remote).",
    ),
    friendly_name: str = typer.Option(None, "--name", help="Optional friendly name"),
    endpoint: list[str] = typer.Option(
        None, "--endpoint",
        help=(
            "Repeatable. ``--endpoint direct=https://10.x.y.z:7820`` or "
            "``--endpoint ssh=alias:port``. Listed-first endpoints are "
            "tried first; the SSH-alias pair is a synthesized last fallback."
        ),
    ),
    tls_fingerprint: str = typer.Option(
        None, "--tls-fingerprint",
        help="SHA-256 of the remote daemon's TLS cert (optional pinning).",
    ),
):
    """Register a remote awm peer reachable via one or more endpoints.

    Each ``--endpoint`` can be:

      ``direct=https://10.147.20.5:7820``  — direct HTTPS to a known IP.

      ``ssh=capella:7820``  — SSH-tunneled (alias passed to OpenSSH).

    The legacy ``--ssh-alias`` + ``--remote-port`` pair is preserved as a
    trailing fallback entry when no explicit ``--endpoint`` is given.
    """
    from awm.services.network import peers as peer_svc

    parsed: list[dict] = []
    for raw in endpoint or []:
        if "=" not in raw:
            typer.echo(f"error: --endpoint must be kind=spec; got {raw!r}", err=True)
            raise typer.Exit(2)
        kind, spec = raw.split("=", 1)
        kind = kind.strip()
        spec = spec.strip()
        if kind == "direct":
            parsed.append({"kind": "direct", "url": spec})
        elif kind == "ssh":
            if ":" not in spec:
                typer.echo(f"error: ssh endpoint must be alias:port; got {spec!r}", err=True)
                raise typer.Exit(2)
            alias, port_s = spec.rsplit(":", 1)
            try:
                port_v = int(port_s)
            except ValueError:
                typer.echo(f"error: ssh endpoint port must be int; got {port_s!r}", err=True)
                raise typer.Exit(2)
            parsed.append({"kind": "ssh", "alias": alias, "port": port_v})
        else:
            typer.echo(f"error: unknown endpoint kind {kind!r}", err=True)
            raise typer.Exit(2)

    if bootstrap_via_ssh and token_file:
        typer.echo("error: --bootstrap-via-ssh and --token-file are mutually exclusive", err=True)
        raise typer.Exit(2)
    if not bootstrap_via_ssh and not token_file:
        typer.echo("error: pass either --token-file <path> or --bootstrap-via-ssh", err=True)
        raise typer.Exit(2)
    if bootstrap_via_ssh and not ssh_alias:
        typer.echo("error: --bootstrap-via-ssh requires --ssh-alias", err=True)
        raise typer.Exit(2)

    try:
        if bootstrap_via_ssh:
            peer_svc.install_peer_token_via_ssh(peer_id, ssh_alias)
        else:
            peer_svc.install_peer_token(peer_id, token_file)
        entry = peer_svc.add_peer(
            peer_id, ssh_alias,
            remote_port=remote_port,
            friendly_name=friendly_name,
            endpoints=parsed or None,
            tls_fingerprint=tls_fingerprint,
        )
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
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


@peer_app.command("set-priority")
def peer_set_priority(
    peer_id: str = typer.Argument(..., help="peer_id (or 'self' to update the local self-row)"),
    priority: int = typer.Argument(..., help="integer priority; lower = higher precedence; 0 wins"),
):
    """Set ``peer_priority`` for leader election.

    Each peer in the cluster should have a unique integer. Lower-numbered
    peers win leadership when reachable; higher-numbered peers stand by.
    """
    from awm.services.network import peers as peer_svc
    target_id = peer_id
    if peer_id == "self":
        ident = peer_svc.get_local_identity()
        if ident is None:
            typer.echo("error: no local peer identity; run `awm peer init` first", err=True)
            raise typer.Exit(2)
        target_id = ident["peer_id"]
    try:
        updated = peer_svc.set_peer_priority(target_id, priority)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2)
    if updated is None:
        typer.echo(f"no such peer: {target_id}", err=True)
        raise typer.Exit(1)
    typer.echo(f"{target_id}: peer_priority = {priority}")


@peer_app.command("refresh-token")
def peer_refresh_token(
    peer_id: str = typer.Argument(...),
    ssh_alias: Optional[str] = typer.Option(
        None, "--ssh-alias",
        help="SSH host alias. Defaults to the alias stored at `awm peer add` time.",
    ),
):
    """Re-fetch a peer's bearer token over SSH after the remote rotated it."""
    from awm.services.network import peers as peer_svc
    entry = peer_svc.get_peer(peer_id)
    if entry is None:
        typer.echo(f"no such peer: {peer_id}", err=True)
        raise typer.Exit(1)
    alias = ssh_alias or entry.get("ssh_alias") or ""
    if not alias:
        typer.echo(
            f"error: peer {peer_id} has no ssh_alias on file — pass --ssh-alias",
            err=True,
        )
        raise typer.Exit(2)
    try:
        path = peer_svc.install_peer_token_via_ssh(peer_id, alias)
    except (ValueError, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2)
    typer.echo(f"refreshed token for {peer_id} via ssh {alias} -> {path}")


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


@inbox_app.command("recipients")
def inbox_recipients(
    query: str = typer.Argument(..., help="Regex matched against recipients, or '*' for all."),
    peer: str = typer.Option(None, "--peer", help="(Reserved; deferred — only 'local' / omitted is honoured today.)"),
):
    """List valid recipient scopes matching a regex (local only)."""
    params: dict[str, object] = {"query": query}
    if peer is not None:
        params["peer"] = peer
    r = _api("GET", "/messages/recipients", params=params)
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


@app.command("vagrant-init")
def vagrant_init():
    """Bootstrap the unified vagrant-scopes bare repo (one-shot, idempotent).

    Creates ``projects/_vagrant/.bare``, the GitHub repo (if ``gh`` is on PATH),
    seeds an initial ``main`` commit, and ensures matching data directories.
    The GitHub remote URL is taken from the config key
    ``vagrant_scopes_repo_url`` (default ``git@github.com:$AWM_GITHUB_USER/vagrant-scopes.git``).
    """
    from awm.services.scopes import ensure_vagrant_repo

    bare = ensure_vagrant_repo()
    typer.echo(f"vagrant-scopes bare repo ready at {bare}")


# ---------------------------------------------------------------------------
# Scope commands
# ---------------------------------------------------------------------------

@scope_app.command("create")
def scope_create(
    project: str = typer.Argument(..., help="Project name"),
    scope: str = typer.Argument(..., help="Scope name"),
    from_branch: Optional[str] = typer.Option(None, "--from", help="Base branch"),
    context: Optional[str] = typer.Option(None, "--context", help="Seed body for `.awm/context.md`"),
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


@scope_app.command("sync")
def scope_sync(
    project: str = typer.Argument(..., help="Project name"),
    scope: str = typer.Argument(..., help="Scope name"),
    strategy: str = typer.Option("merge", "--strategy", help="merge or rebase"),
    from_branch: Optional[str] = typer.Option(None, "--from", help="Base branch (default: project default)"),
):
    """Sync scope's feature branch with its base branch."""
    payload: dict = {"strategy": strategy}
    if from_branch:
        payload["from_branch"] = from_branch
    r = _api("POST", f"/scopes/{project}/{scope}/sync", json=payload)
    _print_json(r)


@scope_app.command("heal")
def scope_heal(
    project: Optional[str] = typer.Option(None, "--project", help="Limit healing to one project"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report intended actions without mutating"),
):
    """Enforce tier-3 = ``.awm/`` only across active scope worktrees.

    For each active scope: strip any leaked ``@.awm/context.md`` line from a
    tracked ``AGENTS.md`` (project-tier doc), delete untracked scope-level
    ``AGENTS.md`` / ``CLAUDE.md`` / ``CLAUDE.md→AGENTS.md`` symlink,
    back-fill ``.awm/context.md`` if missing, and refresh the per-scope
    ``.awm/mcp-opencode.json`` ``instructions`` array to the current
    canonical shape (workspace ``WORKSPACE.md`` + scope ``.awm/context.md``).
    Idempotent. Use ``--dry-run`` to sanity-check the report before mutating.
    """
    from awm.services.scopes import heal_scopes
    import json as _json
    report = heal_scopes(project=project, dry_run=dry_run)
    typer.echo(_json.dumps(report, indent=2))


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



# ---------------------------------------------------------------------------
# Discord operator whitelist
# ---------------------------------------------------------------------------

@discord_app.command("add-operator")
def discord_add_operator(
    discord_user_id: str = typer.Argument(..., help="Discord user ID (snowflake)"),
    awm_user: str = typer.Argument(..., help="awm_user name to map to (e.g. 'tony')"),
):
    """Whitelist a Discord user to run the /login slash command."""
    from awm.db import init_db
    from awm.services.discord import operators

    init_db()
    try:
        entry = operators.add_operator(discord_user_id, awm_user)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2)
    import json as _json
    typer.echo(_json.dumps(entry, indent=2))


@discord_app.command("remove-operator")
def discord_remove_operator(
    discord_user_id: str = typer.Argument(...),
):
    """Remove a Discord operator from the whitelist."""
    from awm.db import init_db
    from awm.services.discord import operators

    init_db()
    if not operators.remove_operator(discord_user_id):
        typer.echo(f"no such operator: {discord_user_id}", err=True)
        raise typer.Exit(1)
    typer.echo(f"removed: {discord_user_id}")


@discord_app.command("list-operators")
def discord_list_operators():
    """List whitelisted Discord operators."""
    from awm.db import init_db
    from awm.services.discord import operators

    init_db()
    import json as _json
    typer.echo(_json.dumps(operators.list_operators(), indent=2))


# ---------------------------------------------------------------------------
# Browser sign-in fallback (no Discord required)
# ---------------------------------------------------------------------------

@app.command()
def login(
    as_user: str = typer.Option("operator", "--as", help="awm_user identity to claim"),
):
    """Mint a one-shot sign-in URL and print it.

    Open the URL in a browser to drop the bearer into an HttpOnly cookie
    and land at ``/ui/``. The nonce expires in 60s and is single-use.
    """
    base, token = _exposed_base_and_token()
    try:
        r = httpx.post(
            f"{base}/auth/mint",
            json={"awm_user": as_user},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
            verify=False,
        )
    except httpx.HTTPError as exc:
        typer.echo(f"could not reach exposed listener at {base}: {exc}", err=True)
        raise typer.Exit(1)
    if r.status_code >= 400:
        typer.echo(f"error ({r.status_code}): {r.text}", err=True)
        raise typer.Exit(1)
    data = r.json()
    typer.echo(data["url"])
    typer.echo(f"# open this in your browser — expires in {data['expires_in_s']}s",
               err=True)


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

    base, token = _exposed_base_and_token()
    try:
        r = httpx.post(
            f"{base}/hub/register",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
            verify=False,
        )
    except httpx.HTTPError as exc:
        typer.echo(f"could not reach hub at {base}: {exc}", err=True)
        raise typer.Exit(1)
    if r.status_code >= 400:
        typer.echo(f"register failed ({r.status_code}): {r.text}", err=True)
        raise typer.Exit(1)
    body = r.json()
    service_id = body["service_id"]
    lease_path = body["lease_ws_path"]
    typer.echo(f"registered {name} → {summary} (id={service_id})")
    typer.echo(f"holding lease at wss://...{lease_path} (Ctrl-C to evict)")

    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = f"{ws_base}{lease_path}"

    ssl_ctx = _ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = _ssl.CERT_NONE

    async def _hold():
        async with _ws.connect(
            ws_url,
            subprotocols=[f"bearer.{token}"],
            ssl=ssl_ctx if ws_url.startswith("wss://") else None,
            max_size=None,
            open_timeout=10,
        ) as wsconn:
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
    r = _exposed_api("GET", "/hub/services")
    if r.status_code >= 400:
        typer.echo(f"error ({r.status_code}): {r.text}", err=True)
        raise typer.Exit(1)
    _print_json(r)


@hub_app.command("deregister")
def hub_deregister(name: str = typer.Argument(..., help="Service name to evict")):
    """Force-evict a service by name (independent of its lease holder)."""
    r = _exposed_api("DELETE", f"/hub/services/{name}")
    if r.status_code >= 400:
        typer.echo(f"error ({r.status_code}): {r.text}", err=True)
        raise typer.Exit(1)
    _print_json(r)


@hub_app.command("trust-self")
def hub_trust_self():
    """Install the local auth token at ``$AWM_DIR/peers/<self>.token``.

    Services authenticate hub→service requests via ``require_peer_bearer``,
    which checks the bearer against the peer-token file for the claimed
    ``X-Awm-From`` peer. The hub forwards as ITSELF, so the local peer's
    own token has to be present in the peers/ directory. Idempotent.
    """
    from awm.services.network import peers as _peers
    from awm.services import auth as _auth
    try:
        token = _auth.local_token(generate_if_missing=False)
    except _auth.TokenMissing as exc:
        typer.echo(f"local auth token missing: {exc}", err=True)
        raise typer.Exit(1)
    local = _peers.get_local_identity()
    if local is None:
        typer.echo("no local peer identity — run `awm peer init` first",
                   err=True)
        raise typer.Exit(1)
    peer_id = local["peer_id"]
    peers_dir = AWM_DIR / "peers"
    peers_dir.mkdir(parents=True, exist_ok=True)
    target = peers_dir / f"{peer_id}.token"
    target.write_text(token + "\n")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    typer.echo(f"trust-self: wrote {target}")


# ---------------------------------------------------------------------------
# Vertical stripes — register packages/* as kind=stripe + hold leases
# ---------------------------------------------------------------------------
#
# A "stripe" is a workspace package (under projects/awm/dev/packages/*)
# whose package.json carries a `stripe` field declaring its frontend
# bundle and optional backend command. `awm stripe sync` walks the
# workspace and registers every stripe with the hub in one process;
# Ctrl-C closes every lease at once and the hub evicts on the next
# tick (which terminates supervised backends — see
# awm.services.hub.supervisor).
#
# Package.json shape (illustrative):
#   {
#     "name": "@awm/hello",
#     "stripe": {
#       "frontend": "dist/",            # absolute or relative-to-package
#       "prefix": "/hello",             # optional; default derived from name
#       "backend": {                    # optional — backendless = frontend-only
#         "cmd": ["node", "dist/server.js", "${AWM_SERVICE_PORT}"],
#         "env": {},
#         "health": "/healthz",
#         "cwd": null
#       }
#     }
#   }

def _stripe_prefix_default(pkg_name: str) -> str:
    """Derive a URL prefix from an npm package name.
    ``@awm/hello`` -> ``/hello``; ``foo-bar`` -> ``/foo-bar``.
    """
    base = pkg_name.split("/", 1)[1] if pkg_name.startswith("@") else pkg_name
    return "/" + base.lstrip("/")


def _load_stripe_package(pkg_dir: pathlib.Path) -> tuple[str, dict]:
    """Read ``<pkg_dir>/package.json``, validate it carries a ``stripe``
    field, return (npm_name, stripe_spec). Raises typer.Exit on
    malformed input."""
    pj_path = pkg_dir / "package.json"
    if not pj_path.is_file():
        typer.echo(f"no package.json at {pj_path}", err=True)
        raise typer.Exit(2)
    try:
        pj = json.loads(pj_path.read_text())
    except json.JSONDecodeError as exc:
        typer.echo(f"package.json at {pj_path} is invalid JSON: {exc}", err=True)
        raise typer.Exit(2)
    name = pj.get("name")
    if not isinstance(name, str) or not name:
        typer.echo(f"{pj_path} missing required field 'name'", err=True)
        raise typer.Exit(2)
    stripe = pj.get("stripe")
    if not isinstance(stripe, dict):
        typer.echo(f"{pj_path} missing 'stripe' field (not a stripe package?)", err=True)
        raise typer.Exit(2)
    return name, stripe


def _stripe_payload(pkg_dir: pathlib.Path, name: str, stripe: dict,
                    name_override: str | None, prefix_override: str | None) -> dict:
    """Build the POST /hub/register payload from a parsed stripe spec.
    Resolves ``frontend`` relative to the package dir."""
    frontend = stripe.get("frontend")
    if not isinstance(frontend, str) or not frontend:
        typer.echo(
            f"stripe in {pkg_dir}/package.json missing 'frontend' (path to bundle dir)",
            err=True,
        )
        raise typer.Exit(2)
    frontend_abs = (pkg_dir / frontend).expanduser().resolve()
    if not frontend_abs.is_dir():
        typer.echo(
            f"frontend dir {frontend_abs} (from {pkg_dir}/package.json) does not exist — build first?",
            err=True,
        )
        raise typer.Exit(2)

    reg_name = name_override or name
    reg_prefix = prefix_override or stripe.get("prefix") or _stripe_prefix_default(name)

    stripe_payload: dict = {"dir": str(frontend_abs)}
    backend = stripe.get("backend")
    if backend is not None:
        if not isinstance(backend, dict):
            typer.echo(f"stripe.backend in {pkg_dir}/package.json must be an object", err=True)
            raise typer.Exit(2)
        cmd = backend.get("cmd")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd):
            typer.echo(
                f"stripe.backend.cmd in {pkg_dir}/package.json must be a non-empty list of strings",
                err=True,
            )
            raise typer.Exit(2)
        backend_payload: dict = {"cmd": list(cmd)}
        if "env" in backend:
            if not isinstance(backend["env"], dict):
                typer.echo(
                    f"stripe.backend.env in {pkg_dir}/package.json must be an object",
                    err=True,
                )
                raise typer.Exit(2)
            backend_payload["env"] = {str(k): str(v) for k, v in backend["env"].items()}
        if "health" in backend:
            backend_payload["health"] = str(backend["health"])
        # cwd defaults (hub side) to the frontend dir; allow override.
        if backend.get("cwd"):
            cwd = (pkg_dir / backend["cwd"]).expanduser().resolve()
            backend_payload["cwd"] = str(cwd)
        stripe_payload["backend"] = backend_payload

    return {"name": reg_name, "prefix": reg_prefix, "stripe": stripe_payload}


async def _hold_one_lease(base: str, token: str, name: str, lease_path: str) -> None:
    """Open the lease WS and idle until close. Used by both
    ``stripe register`` (one lease) and ``stripe sync`` (N concurrent).
    """
    import ssl as _ssl
    import websockets as _ws

    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = f"{ws_base}{lease_path}"
    ssl_ctx = _ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = _ssl.CERT_NONE
    async with _ws.connect(
        ws_url,
        subprotocols=[f"bearer.{token}"],
        ssl=ssl_ctx if ws_url.startswith("wss://") else None,
        max_size=None,
        open_timeout=10,
    ) as wsconn:
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


def _post_register(base: str, token: str, payload: dict) -> dict:
    """POST /hub/register with the given payload, return parsed body
    or typer.Exit(1) on error."""
    try:
        r = httpx.post(
            f"{base}/hub/register",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
            verify=False,
        )
    except httpx.HTTPError as exc:
        typer.echo(f"could not reach hub at {base}: {exc}", err=True)
        raise typer.Exit(1)
    if r.status_code >= 400:
        typer.echo(f"register failed ({r.status_code}): {r.text}", err=True)
        raise typer.Exit(1)
    return r.json()


@stripe_app.command("register")
def stripe_register(
    package: str = typer.Option(
        ..., "--package",
        help="Path to a stripe package directory (must contain package.json "
             "with a 'stripe' field).",
    ),
    name: str | None = typer.Option(
        None, "--name",
        help="Override the registration name (default: package.json 'name'). "
             "Use this to coexist with the same stripe synced from another scope.",
    ),
    prefix: str | None = typer.Option(
        None, "--prefix",
        help="Override the URL prefix (default: derived from package name).",
    ),
):
    """Register one stripe package with the hub and hold its lease.

    Ctrl-C closes the lease → hub evicts → supervised backend is
    SIGTERM'd. Re-running re-registers from scratch.
    """
    import asyncio as _asyncio

    pkg_dir = pathlib.Path(package).expanduser().resolve()
    if not pkg_dir.is_dir():
        typer.echo(f"package dir {pkg_dir} does not exist", err=True)
        raise typer.Exit(2)

    pkg_name, stripe_spec = _load_stripe_package(pkg_dir)
    payload = _stripe_payload(pkg_dir, pkg_name, stripe_spec, name, prefix)

    base, token = _exposed_base_and_token()
    body = _post_register(base, token, payload)
    typer.echo(
        f"registered stripe {payload['name']} → prefix={payload['prefix']} "
        f"id={body['service_id']}"
    )
    typer.echo(f"holding lease (Ctrl-C to evict)…")
    try:
        _asyncio.run(_hold_one_lease(base, token, payload["name"], body["lease_ws_path"]))
    except KeyboardInterrupt:
        typer.echo("lease closed — stripe evicted")


@stripe_app.command("sync")
def stripe_sync(
    workspace: str = typer.Argument(
        ...,
        help="Path to the npm workspaces root containing 'packages/'. "
             "Every packages/<name>/package.json with a 'stripe' field is "
             "registered.",
    ),
    prefix_prefix: str | None = typer.Option(
        None, "--prefix-prefix",
        help="Optional URL prefix to prepend to every stripe's prefix "
             "(e.g. '/dev' to namespace stripes under /dev/<name>).",
    ),
):
    """Discover every stripe under ``<workspace>/packages/*`` and register
    them all in one process, holding N concurrent leases.

    Ctrl-C closes every lease → hub evicts every stripe at once.
    Failures to register an individual stripe are reported but do not
    abort the others.
    """
    import asyncio as _asyncio

    ws_root = pathlib.Path(workspace).expanduser().resolve()
    pkgs_root = ws_root / "packages"
    if not pkgs_root.is_dir():
        typer.echo(f"no packages/ dir at {pkgs_root}", err=True)
        raise typer.Exit(2)

    base, token = _exposed_base_and_token()
    registered: list[tuple[str, str]] = []   # (name, lease_path)

    for pkg_dir in sorted(p for p in pkgs_root.iterdir() if p.is_dir()):
        pj = pkg_dir / "package.json"
        if not pj.is_file():
            continue
        try:
            data = json.loads(pj.read_text())
        except json.JSONDecodeError:
            typer.echo(f"skip {pkg_dir.name}: package.json is invalid JSON", err=True)
            continue
        if not isinstance(data.get("stripe"), dict):
            # Library packages (e.g. @awm/bus) declare no `stripe` field.
            continue
        name = data["name"]
        try:
            payload = _stripe_payload(pkg_dir, name, data["stripe"], None, None)
            if prefix_prefix:
                payload["prefix"] = prefix_prefix.rstrip("/") + payload["prefix"]
            body = _post_register(base, token, payload)
        except typer.Exit:
            typer.echo(f"skip {name}: registration failed (see prior error)", err=True)
            continue
        registered.append((payload["name"], body["lease_ws_path"]))
        typer.echo(
            f"registered {payload['name']} → prefix={payload['prefix']} "
            f"id={body['service_id']}"
        )

    if not registered:
        typer.echo("no stripe packages found", err=True)
        raise typer.Exit(1)

    typer.echo(f"holding {len(registered)} lease(s) (Ctrl-C to evict all)…")
    async def _hold_all():
        await _asyncio.gather(*(
            _hold_one_lease(base, token, name, path) for name, path in registered
        ))
    try:
        _asyncio.run(_hold_all())
    except KeyboardInterrupt:
        typer.echo("all leases closed — stripes evicted")


@stripe_app.command("list")
def stripe_list():
    """List currently registered stripes (status + per-stripe URLs)."""
    r = _exposed_api("GET", "/hub/stripes")
    if r.status_code >= 400:
        typer.echo(f"error ({r.status_code}): {r.text}", err=True)
        raise typer.Exit(1)
    _print_json(r)
