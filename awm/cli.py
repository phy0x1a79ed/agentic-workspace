"""Typer CLI app with auto-start logic."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
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
task_app = typer.Typer(help="Task management", no_args_is_help=True)
lock_app = typer.Typer(help="Lock management", no_args_is_help=True)
shared_app = typer.Typer(help="Shared resource edits", no_args_is_help=True)

app.add_typer(project_app, name="project")
app.add_typer(task_app, name="task")
app.add_typer(lock_app, name="lock")
app.add_typer(shared_app, name="shared")


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
    for d in ["data/reference", "results", "reports", "tasks", "tasks_active",
              "skills/sops", "skills/tools", "skills/templates", "scripts"]:
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
    """Show server health + active locks + tasks summary."""
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
# Task commands
# ---------------------------------------------------------------------------

@task_app.command("create")
def task_create(
    project: str = typer.Argument(..., help="Project name"),
    task: str = typer.Argument(..., help="Task name"),
    from_branch: Optional[str] = typer.Option(None, "--from", help="Base branch"),
):
    """Create a task worktree."""
    payload = {"project": project, "task": task}
    if from_branch:
        payload["from_branch"] = from_branch
    r = _api("POST", "/tasks", json=payload)
    _print_json(r)


@task_app.command("complete")
def task_complete(
    project: str = typer.Argument(..., help="Project name"),
    task: str = typer.Argument(..., help="Task name"),
    merge: bool = typer.Option(False, "--merge", help="Merge feature branch into main"),
):
    """Complete a task."""
    r = _api("PATCH", f"/tasks/{project}/{task}", json={"action": "complete", "merge": merge})
    _print_json(r)


@task_app.command("pause")
def task_pause(
    project: str = typer.Argument(..., help="Project name"),
    task: str = typer.Argument(..., help="Task name"),
):
    """Pause a task."""
    r = _api("PATCH", f"/tasks/{project}/{task}", json={"action": "pause"})
    _print_json(r)


@task_app.command("resume")
def task_resume(
    project: str = typer.Argument(..., help="Project name"),
    task: str = typer.Argument(..., help="Task name"),
):
    """Resume a paused task."""
    r = _api("PATCH", f"/tasks/{project}/{task}", json={"action": "resume"})
    _print_json(r)


@task_app.command("list")
def task_list(
    status: Optional[str] = typer.Option(None, "--status", help="Filter by status (active/completed/paused/all)"),
    project: Optional[str] = typer.Option(None, "--project", help="Filter by project"),
):
    """List tasks."""
    params = {}
    if status:
        params["status"] = status
    if project:
        params["project"] = project
    r = _api("GET", "/tasks", params=params)

    data = r.json()
    if r.status_code >= 400:
        typer.echo(f"Error: {data}", err=True)
        raise typer.Exit(1)

    tasks = data["tasks"]
    if not tasks:
        typer.echo("(no tasks found)")
        return

    # Table output
    typer.echo(f"{'PROJECT':<20} {'TASK':<25} {'STATUS':<12} {'BRANCH':<30}")
    typer.echo(f"{'-------':<20} {'----':<25} {'------':<12} {'------':<30}")
    for t in tasks:
        typer.echo(f"{t['project']:<20} {t['task']:<25} {t['status']:<12} {t['branch']:<30}")
    typer.echo(f"\nTotal: {data['total']} task(s)")


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
