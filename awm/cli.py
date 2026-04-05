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

app.add_typer(project_app, name="project")
app.add_typer(scope_app, name="scope")
app.add_typer(lock_app, name="lock")
app.add_typer(shared_app, name="shared")
app.add_typer(skill_app, name="skill")


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
    r = subprocess.run(
        ["systemctl", "--user", "restart", "awm.service"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        typer.echo(f"systemctl restart failed: {r.stderr.strip() or r.stdout.strip()}", err=True)
        typer.echo("Hint: install the unit at ~/.config/systemd/user/awm.service first "
                   "(see projects/awm/release/deploy/awm.service).", err=True)
        raise typer.Exit(r.returncode)
    typer.echo("awm core restarted. MCP clients reconnect on next tool call.")


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

@skill_app.command("list")
def skill_list(
    type: Optional[str] = typer.Option(None, "--type", help="Filter by type (sop, tool, template)"),
    tags: Optional[str] = typer.Option(None, "--tags", help="Comma-separated tags to filter by"),
):
    """List all skills in the catalog."""
    params = {}
    if type:
        params["type"] = type
    if tags:
        params["tags"] = tags
    r = _api("GET", "/skills", params=params)

    data = r.json()
    if r.status_code >= 400:
        typer.echo(f"Error: {data}", err=True)
        raise typer.Exit(1)

    skills = data["skills"]
    if not skills:
        typer.echo("(no skills found)")
        return

    typer.echo(f"{'NAME':<25} {'TYPE':<10} {'TAGS':<30} {'FILE':<35}")
    typer.echo(f"{'----':<25} {'----':<10} {'----':<30} {'----':<35}")
    for s in skills:
        tags_str = ", ".join(s.get("tags", []))
        typer.echo(f"{s['name']:<25} {s['type']:<10} {tags_str:<30} {s['file_path']:<35}")
    typer.echo(f"\nTotal: {data['total']} skill(s)")


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
):
    """Search skills by name, tags, description, or content."""
    r = _api("GET", "/skills/search", params={"q": query})

    data = r.json()
    if r.status_code >= 400:
        typer.echo(f"Error: {data}", err=True)
        raise typer.Exit(1)

    skills = data["skills"]
    if not skills:
        typer.echo("(no matching skills)")
        return

    typer.echo(f"{'NAME':<25} {'TYPE':<10} {'FILE':<35}")
    typer.echo(f"{'----':<25} {'----':<10} {'----':<35}")
    for s in skills:
        typer.echo(f"{s['name']:<25} {s['type']:<10} {s['file_path']:<35}")
    typer.echo(f"\nTotal: {data['total']} match(es)")


@skill_app.command("reindex")
def skill_reindex():
    """Regenerate the skills/_index.md from a live scan."""
    r = _api("POST", "/skills/reindex")
    if r.status_code >= 400:
        typer.echo(f"Error ({r.status_code}): {r.text}", err=True)
        raise typer.Exit(1)
    typer.echo("Skills index regenerated.")


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
