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
inbox_app = typer.Typer(help="Inbox: send and read scoped messages", no_args_is_help=True)
room_app = typer.Typer(help="Rooms: multi-participant conversations with agents", no_args_is_help=True)

discord_app = typer.Typer(help="Discord bot operator whitelist", no_args_is_help=True)
hub_app = typer.Typer(help="Service hub: register + lease external services", no_args_is_help=True)
packages_app = typer.Typer(help="Packages: generate manifests + sync packages/{services,pages}/ with the hub", no_args_is_help=True)
dev_app = typer.Typer(help="Dev workflows: shadow packages into the running hub", no_args_is_help=True)

context_app = typer.Typer(help="Scope context: emit .awm/context.md for harness SessionStart hooks", no_args_is_help=True)

app.add_typer(project_app, name="project")
app.add_typer(scope_app, name="scope")
app.add_typer(lock_app, name="lock")
app.add_typer(shared_app, name="shared")
app.add_typer(skill_app, name="skill")
app.add_typer(inbox_app, name="inbox")
app.add_typer(room_app, name="room")
app.add_typer(discord_app, name="discord")
app.add_typer(context_app, name="context")
app.add_typer(hub_app, name="hub")
app.add_typer(packages_app, name="packages")
app.add_typer(dev_app, name="dev")


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


def _exposed_api(method: str, path: str, **kwargs) -> httpx.Response:
    """Hit an endpoint on the local awm-exposed listener."""
    base, token = _exposed_base_and_token()
    headers = kwargs.pop("headers", {}) or {}
    headers["Authorization"] = f"Bearer {token}"
    # Self-signed TLS on loopback — bearer is the trust boundary.
    r = httpx.request(
        method, f"{base}{path}", headers=headers, timeout=30, verify=False,
        **kwargs,
    )
    return r


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


@app.command()
def status():
    """Show server health + active locks + scopes summary."""
    r = _api("GET", "/status")
    _print_json(r)


@inbox_app.command("send")
def inbox_send(
    scope: str = typer.Argument(..., help="Target scope, e.g. 'scope:foo/bar'"),
    subject: str = typer.Option(..., "--subject"),
    body: str = typer.Option(..., "--body"),
    sender: str = typer.Option("operator", "--sender"),
    msg_type: str = typer.Option("notification", "--type",
                                  help="One of: scope_assignment, reflection, status_update, notification, plan"),
    metadata: str = typer.Option(None, "--metadata", help="Optional JSON-encoded metadata"),
):
    """Send a message to a local scope inbox."""
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
    """Fetch full messages from a scope."""
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
    """Search message previews."""
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
):
    """List valid recipient scopes matching a regex."""
    params: dict[str, object] = {"query": query}
    r = _api("GET", "/messages/recipients", params=params)
    _print_json(r)


@room_app.command("create")
def room_create(
    topic: str = typer.Option(None, "--topic", help="Optional human-readable topic"),
    scope: list[str] = typer.Option(
        None, "--scope",
        help="Scope to enroll (project/scope); repeatable",
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


@room_app.command("get")
def room_get(name: str = typer.Argument(...)):
    """Show room details, participants, and recent transcript."""
    r = _exposed_api("GET", f"/rooms/{name}")
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
    r = _exposed_api("GET", f"/rooms/{name}/history", params=params)
    _print_json(r)


@room_app.command("search")
def room_search(
    query: Optional[str] = typer.Argument(None, help="Free-text query (optional)"),
    status: str = typer.Option("active", "--status", help="active (default), closed, archived, or all"),
    participating_scope: Optional[str] = typer.Option(None, "--participating-scope"),
    limit: int = typer.Option(50, "--limit"),
    offset: int = typer.Option(0, "--offset"),
):
    """Search rooms (hybrid keyword + semantic). Defaults to status='active'."""
    params: dict = {"status": status, "limit": limit, "offset": offset}
    if query:
        params["query"] = query
    if participating_scope:
        params["participating_scope"] = participating_scope
    r = _exposed_api("GET", "/rooms/search", params=params)
    _print_json(r)


@room_app.command("post")
def room_post(
    name: str = typer.Argument(...),
    text: str = typer.Argument(...),
    to_scope: str = typer.Option(None, "--to", help="Direct-address a scope"),
):
    """Post a message to a room (as the local operator)."""
    r = _exposed_api("POST", f"/rooms/{name}/posts", json={"body": text, "to": to_scope})
    _print_json(r)


@room_app.command("invite")
def room_invite(
    name: str = typer.Argument(...),
    scope: str = typer.Option(..., "--scope"),
    prompt: str = typer.Option(None, "--prompt"),
):
    """Invite a scope (agent) into a room."""
    r = _exposed_api(
        "POST", f"/rooms/{name}/invite",
        json={"scope": scope, "prompt": prompt},
    )
    _print_json(r)


@room_app.command("remove")
def room_remove(
    name: str = typer.Argument(...),
    scope: str = typer.Option(..., "--scope"),
):
    """Remove a scope from a room (doesn't kill the agent process)."""
    r = _exposed_api("POST", f"/rooms/{name}/remove", json={"scope": scope})
    _print_json(r)


@room_app.command("close")
def room_close(
    name: str = typer.Argument(...),
    kill_agents: bool = typer.Option(False, "--kill-agents"),
):
    """Close a room (optionally SIGTERM all participant agents)."""
    r = _exposed_api(
        "POST", f"/rooms/{name}/close", json={"kill_agents": kill_agents},
    )
    _print_json(r)


@room_app.command("archive")
def room_archive(name: str = typer.Argument(...)):
    """Soft-archive a room. Refused (409) if active scope participants remain."""
    r = _exposed_api("POST", f"/rooms/{name}/archive")
    _print_json(r)


@room_app.command("agents")
def room_agents(name: str = typer.Argument(...)):
    """List room participants with live agent state (PID, status)."""
    r = _exposed_api("GET", f"/rooms/{name}/agents")
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

    base_url, token = _exposed_base_and_token()
    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
    uri = f"{ws_url}/rooms/{name}/attach"

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


@project_app.command("search")
def project_search(
    query: Optional[str] = typer.Option(None, "--query", help="Free-text query (project name + README)"),
    active_only: bool = typer.Option(False, "--active-only", help="Hide projects with zero active scopes"),
    limit: int = typer.Option(50, "--limit"),
    offset: int = typer.Option(0, "--offset"),
):
    """Search projects (hybrid keyword + semantic) with per-status scope counts."""
    params: dict = {"limit": limit, "offset": offset}
    if query:
        params["query"] = query
    if active_only:
        params["active_only"] = "true"
    r = _api("GET", "/projects/search", params=params)
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


@scope_app.command("search")
def scope_search(
    query: Optional[str] = typer.Option(None, "--query", help="Free-text query (scope name + context.md)"),
    status: str = typer.Option("active", "--status", help="active (default), completed, deleted, or all"),
    project: Optional[str] = typer.Option(None, "--project", help="Filter by project"),
    limit: int = typer.Option(50, "--limit"),
    offset: int = typer.Option(0, "--offset"),
):
    """Search scopes (hybrid keyword + semantic). Defaults to status='active'."""
    params: dict = {"status": status, "limit": limit, "offset": offset}
    if query:
        params["query"] = query
    if project:
        params["project"] = project
    r = _api("GET", "/scopes/search", params=params)

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


@lock_app.command("search")
def lock_search(
    query: Optional[str] = typer.Option(None, "--query", help="Free-text query (resource path + holder + metadata)"),
    status: str = typer.Option("active", "--status", help="active (default), stale, or all"),
    holder: Optional[str] = typer.Option(None, "--holder", help="Filter by holder"),
    path: Optional[str] = typer.Option(None, "--path", help="Filter by path"),
    limit: int = typer.Option(50, "--limit"),
    offset: int = typer.Option(0, "--offset"),
):
    """Search locks (hybrid keyword + semantic). Defaults to status='active'
    (live PID + fresh heartbeat); pass status='stale' or 'all'."""
    params: dict = {"status": status, "limit": limit, "offset": offset}
    if query:
        params["query"] = query
    if holder:
        params["holder"] = holder
    if path:
        params["path"] = path
    r = _api("GET", "/locks/search", params=params)

    data = r.json()
    if r.status_code >= 400:
        typer.echo(f"Error: {data}", err=True)
        raise typer.Exit(1)

    locks = data["locks"]
    if not locks:
        typer.echo("(no matching locks)")
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
    query: Optional[str] = typer.Argument(None, help="Free-text query (optional; omit to filter only)"),
    type: Optional[str] = typer.Option(None, "--type", help="Filter by type (sop, tool, template)"),
    tags: Optional[str] = typer.Option(None, "--tags", help="Comma-separated tags to filter by"),
):
    """Search skills (hybrid keyword + semantic) with optional type/tag filters."""
    params: dict = {}
    if query:
        params["query"] = query
    if type:
        params["type"] = type
    if tags:
        params["tags"] = tags

    r = _api("GET", "/skills/search", params=params)
    if r.status_code >= 400:
        typer.echo(f"Error: {r.text}", err=True)
        raise typer.Exit(1)
    data = r.json()

    skills = data["skills"]
    if not skills:
        typer.echo("(no matching skills)")
        return

    typer.echo(f"{'NAME':<25} {'TYPE':<10} {'TAGS':<30} {'FILE':<35}")
    typer.echo(f"{'----':<25} {'----':<10} {'----':<30} {'----':<35}")
    for s in skills:
        tags_str = ", ".join(s.get("tags", []))
        typer.echo(f"{s['name']:<25} {s['type']:<10} {tags_str:<30} {s['file_path']:<35}")
    typer.echo(f"\nTotal: {data['total']} skill(s)")


# ---------------------------------------------------------------------------
# Session commands — registered via registry + migrate-experiences
# ---------------------------------------------------------------------------

from awm.operations.sessions import SESSION_OPERATIONS
from awm._lib.operations import register_cli_commands

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
def hub_deregister(
    name: str = typer.Argument(..., help="Service name to evict"),
    kind: str = typer.Option(None, "--kind", help="Disambiguate if the name exists across multiple kinds (page|service|url|static)"),
):
    """Force-evict a service by name (independent of its lease holder)."""
    path = f"/hub/services/{name}"
    if kind:
        path += f"?kind={kind}"
    r = _exposed_api("DELETE", path)
    if r.status_code >= 400:
        typer.echo(f"error ({r.status_code}): {r.text}", err=True)
        raise typer.Exit(1)
    _print_json(r)




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
#   their start.sh is invoked with AWM_HUB_URL + AWM_HUB_TOKEN in env so
#   they can call /hub/service/register themselves.
#
# `awm dev shadow services/tts pages/dashboard ...` — from a scope worktree,
#   push the same-prefix packages as overlays onto the dev sandbox's hub;
#   Ctrl-C pops them (no respawn — base traffic resumes).


async def _hold_one_lease(base: str, token: str, name: str, lease_path: str) -> None:
    """Open the lease WS and idle until close. Used by packages sync (N
    concurrent leases) and dev shadow (per-overlay lease)."""
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


@packages_app.command("gen")
def packages_gen(
    repo_root: str = typer.Argument(
        ".",
        help="Workspace root containing packages/. Defaults to cwd.",
    ),
):
    """Generate per-package package.json + per-page vite.config.ts from the
    packages/{components,pages}/<name>/ layout. Run before npm install."""
    from awm.services.packages import gen as _gen
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


def _post_service_register(base: str, token: str, payload: dict) -> dict:
    try:
        r = httpx.post(
            f"{base}/hub/service/register",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
            verify=False,
        )
    except httpx.HTTPError as exc:
        typer.echo(f"could not reach hub at {base}: {exc}", err=True)
        raise typer.Exit(1)
    if r.status_code >= 400:
        typer.echo(f"service register failed ({r.status_code}): {r.text}",
                   err=True)
        raise typer.Exit(1)
    return r.json()


def _post_page_register(base: str, token: str, name: str, prefix: str,
                        dir_: str) -> dict:
    payload = {
        "name": name,
        "prefix": prefix,
        "page": {"dir": dir_},
    }
    return _post_register(base, token, payload)


def _read_prefix_txt(pkg_dir: pathlib.Path, default: str) -> str:
    f = pkg_dir / "prefix.txt"
    if f.is_file():
        text = f.read_text(encoding="utf-8").strip()
        if text:
            return text if text.startswith("/") else "/" + text
    return default


def _spawn_service_local(pkg_dir: pathlib.Path, base: str, token: str) -> int:
    """Spawn ``start.sh`` for a service with hub env vars injected.

    Returns the PID. The service is expected to POST /hub/service/register
    on startup; if it doesn't reconnect within the 10s window the hub
    will SIGTERM this PID and respawn from start.sh itself (S4).
    """
    env = os.environ.copy()
    env["AWM_HUB_URL"] = base
    env["AWM_HUB_TOKEN"] = token
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

    For each service: spawns ``start.sh`` with AWM_HUB_URL/AWM_HUB_TOKEN in
    env; the service then registers itself + opens its control WS.

    For each page: POST /hub/register with the static spec at prefix
    ``/ui/<name>``; this command holds one lease per page until Ctrl-C.
    """
    import asyncio as _asyncio

    ws_root = pathlib.Path(workspace).expanduser().resolve()
    services, pages = _packages_walk(ws_root)
    if not services and not pages:
        typer.echo("no service or page packages found", err=True)
        raise typer.Exit(1)

    base, token = _exposed_base_and_token()
    leases: list[tuple[str, str]] = []
    spawned_pids: list[int] = []

    for svc_dir in services:
        name = svc_dir.name
        try:
            spawned_pids.append(_spawn_service_local(svc_dir, base, token))
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
            body = _post_page_register(base, token, name, prefix, str(dist))
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
                _hold_one_lease(base, token, name, path) for name, path in leases
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
    r = _exposed_api("GET", "/hub/services")
    if r.status_code >= 400:
        typer.echo(f"error ({r.status_code}): {r.text}", err=True)
        raise typer.Exit(1)
    _print_json(r)


@dev_app.command("shadow")
def dev_shadow(
    targets: list[str] = typer.Argument(
        ...,
        help="One or more package shadows of the form "
             "'services/<name>' or 'pages/<name>'. Components are not "
             "shadowable directly (they're build-time deps).",
    ),
):
    """Push selected packages as shadow overlays onto the running hub.

    Resolves each target relative to the current scope worktree
    (``./packages/<target>/``), then POSTs /hub/shadow/register once per
    target and holds one lease each. Ctrl-C pops the overlays; base
    traffic resumes instantly with no respawn or warmup.
    """
    import asyncio as _asyncio

    here = pathlib.Path.cwd()
    base, token = _exposed_base_and_token()
    leases: list[tuple[str, str]] = []
    spawned_pids: list[int] = []

    for target in targets:
        target = target.strip().lstrip("/")
        if "/" not in target:
            typer.echo(f"skip {target!r}: expected 'services/<name>' or "
                       f"'pages/<name>'", err=True)
            continue
        kind, name = target.split("/", 1)
        pkg_dir = (here / "packages" / kind / name).expanduser().resolve()
        if not pkg_dir.is_dir():
            typer.echo(f"skip {target}: {pkg_dir} not a directory", err=True)
            continue
        shadow_name = f"shadow:{name}:{pkg_dir.parent.parent.parent.name}"
        if kind == "components":
            typer.echo(f"skip {target}: components are build-time deps; "
                       f"rebuild + shadow the page that imports them instead",
                       err=True)
            continue
        if kind == "pages":
            dist = pkg_dir / "dist"
            if not dist.is_dir():
                typer.echo(f"skip {target}: no dist/ — build first", err=True)
                continue
            prefix = _read_prefix_txt(pkg_dir, f"/ui/{name}")
            payload = {
                "name": shadow_name,
                "prefix": prefix,
                "page": {"dir": str(dist)},
            }
        elif kind == "services":
            if not (pkg_dir / "start.sh").is_file():
                typer.echo(f"skip {target}: no start.sh", err=True)
                continue
            try:
                pid = _spawn_service_local(pkg_dir, base, token)
                spawned_pids.append(pid)
            except (OSError, ValueError) as exc:
                typer.echo(f"skip {target}: spawn failed: {exc}", err=True)
                continue
            payload = {
                "name": shadow_name,
                "prefix": f"/svc/{name}",
                "service": {
                    "name": shadow_name,
                    "prefix": f"/svc/{name}",
                    "pid": pid,
                    "start": ["bash", str(pkg_dir / "start.sh")],
                    "cwd": str(pkg_dir),
                },
            }
        else:
            typer.echo(f"skip {target}: unknown kind {kind!r}", err=True)
            continue

        try:
            r = httpx.post(
                f"{base}/hub/shadow/register",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
                verify=False,
            )
        except httpx.HTTPError as exc:
            typer.echo(f"skip {target}: hub unreachable: {exc}", err=True)
            continue
        if r.status_code >= 400:
            typer.echo(f"skip {target}: shadow register failed "
                       f"({r.status_code}): {r.text}", err=True)
            continue
        body = r.json()
        leases.append((target, body["lease_ws_path"]))
        typer.echo(f"shadow {target} → prefix={payload['prefix']} "
                   f"id={body['service_id']}")

    if not leases:
        # Clean up any spawned services if no overlay actually landed.
        for pid in spawned_pids:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        typer.echo("no shadows pushed", err=True)
        raise typer.Exit(1)

    typer.echo(f"holding {len(leases)} shadow lease(s) (Ctrl-C to pop)…")
    async def _hold_all():
        await _asyncio.gather(*(
            _hold_one_lease(base, token, name, path) for name, path in leases
        ))
    try:
        _asyncio.run(_hold_all())
    except KeyboardInterrupt:
        typer.echo("all shadow leases closed — base traffic resumes")
    finally:
        for pid in spawned_pids:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
