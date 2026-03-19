"""MCP stdio server for the Agentic Workspace Manager.

Exposes AWM services as MCP tools so Claude Code (and other MCP clients)
can manage projects, tasks, locks, skills, and sessions.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from awm.db import init_db
from awm.models import (
    AgentSpawnRequest,
    LockAcquireRequest,
    MessageSendRequest,
    ProjectCreateRequest,
    TaskCreateRequest,
    TaskUpdateRequest,
)
from awm.operations.sessions import SESSION_OPERATIONS
from awm.registry import dispatch_operation, operations_to_mcp_tools
from awm.services import agents, locks, messaging, projects, skills, tasks

server = Server("awm")


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    # Skills
    Tool(
        name="skills_list",
        description="List skills in the catalog, optionally filtered by type or tags.",
        inputSchema={
            "type": "object",
            "properties": {
                "type": {"type": "string", "description": "Filter by skill type (sop, tool, template)"},
                "tags": {"type": "string", "description": "Comma-separated tags to filter by"},
            },
        },
    ),
    Tool(
        name="skills_get",
        description="Read a skill file by relative path (e.g. 'sops/git-workflow.md').",
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to the skill file"},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="skills_search",
        description="Search skills by name, tags, description, or content.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="skills_reindex",
        description="Regenerate the skills/_index.md from a live scan of the skills directory.",
        inputSchema={"type": "object", "properties": {}},
    ),
    # Sessions — from registry
    *operations_to_mcp_tools(SESSION_OPERATIONS),
    # Tasks
    Tool(
        name="task_create",
        description="Create a new task worktree for a project.",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "task": {"type": "string"},
                "from_branch": {"type": "string"},
                "context": {"type": "string", "description": "Seed content for AGENTS.md (task context/instructions)"},
            },
            "required": ["project", "task"],
        },
    ),
    Tool(
        name="task_list",
        description="List tasks, optionally filtered by status and/or project.",
        inputSchema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "active, completed, deleted, or all"},
                "project": {"type": "string"},
            },
        },
    ),
    Tool(
        name="task_complete",
        description="Complete a task, optionally merging the feature branch and/or cleaning up the worktree.",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "task": {"type": "string"},
                "merge": {"type": "boolean", "default": False},
                "cleanup": {"type": "boolean", "default": False, "description": "Remove worktree and branch after completion"},
            },
            "required": ["project", "task"],
        },
    ),
    Tool(
        name="task_delete",
        description="Delete a task — clean up its worktree, branch, and mark as deleted in DB.",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "task": {"type": "string"},
            },
            "required": ["project", "task"],
        },
    ),
    # Projects
    Tool(
        name="project_create",
        description="Create a new project with bare repo, worktree, and data directories.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "clone_url": {"type": "string"},
                "fork_url": {"type": "string"},
            },
            "required": ["name"],
        },
    ),
    # Locks
    Tool(
        name="lock_acquire",
        description="Acquire a lock on a resource path.",
        inputSchema={
            "type": "object",
            "properties": {
                "resource_path": {"type": "string"},
                "holder_id": {"type": "string"},
                "lock_type": {"type": "string", "default": "exclusive"},
                "holder_pid": {"type": "integer"},
                "metadata": {"type": "string"},
            },
            "required": ["resource_path", "holder_id"],
        },
    ),
    Tool(
        name="lock_release",
        description="Release a lock on a resource path.",
        inputSchema={
            "type": "object",
            "properties": {
                "resource_path": {"type": "string"},
                "holder_id": {"type": "string"},
            },
            "required": ["resource_path", "holder_id"],
        },
    ),
    Tool(
        name="lock_list",
        description="List active locks, optionally filtered by holder or path.",
        inputSchema={
            "type": "object",
            "properties": {
                "holder_id": {"type": "string"},
                "path": {"type": "string"},
            },
        },
    ),
    Tool(
        name="lock_heartbeat",
        description="Renew heartbeat for all locks held by a given holder.",
        inputSchema={
            "type": "object",
            "properties": {
                "holder_id": {"type": "string"},
            },
            "required": ["holder_id"],
        },
    ),
    # Messaging
    Tool(
        name="inbox_send",
        description="Send a message to a scoped inbox (workspace, project:X, or task:X/Y).",
        inputSchema={
            "type": "object",
            "properties": {
                "scope": {"type": "string", "description": "Target scope: 'workspace', 'project:X', or 'task:X/Y'"},
                "sender": {"type": "string", "description": "Sender identifier (agent name or scope)"},
                "msg_type": {"type": "string", "enum": ["task_assignment", "reflection", "status_update", "notification", "plan"]},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "metadata": {"type": "string", "description": "Optional JSON metadata"},
            },
            "required": ["scope", "sender", "msg_type", "subject", "body"],
        },
    ),
    Tool(
        name="inbox_search",
        description="Search/filter messages by scope, status, msg_type, or free-text query.",
        inputSchema={
            "type": "object",
            "properties": {
                "scope": {"type": "string", "description": "Filter by scope"},
                "status": {"type": "string", "description": "Filter by status: unread or read"},
                "msg_type": {"type": "string", "description": "Filter by message type"},
                "query": {"type": "string", "description": "Free-text search across subject and body"},
                "limit": {"type": "integer", "default": 50},
            },
        },
    ),
    Tool(
        name="inbox_read",
        description="Mark a message as read by ID.",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "Message ID to mark as read"},
            },
            "required": ["id"],
        },
    ),
    Tool(
        name="inbox_recipients",
        description="List valid recipient scopes (workspace + all projects + all tasks).",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="agent_spawn",
        description="Spawn a fire-and-forget agent subprocess on a task workspace.",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "task": {"type": "string"},
                "prompt": {"type": "string", "description": "Optional prompt/plan for the agent (sent to task inbox)"},
                "agent_cli": {"type": "string", "description": "CLI to use: 'opencode' or 'claude' (default from config)"},
            },
            "required": ["project", "task"],
        },
    ),
    # Status
    Tool(
        name="awm_status",
        description="Get AWM server status: workspace root, active locks, tasks, and shared edits.",
        inputSchema={"type": "object", "properties": {}},
    ),
]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def _serialize(obj: Any) -> str:
    """Serialize a Pydantic model (or dict) to JSON string."""
    if hasattr(obj, "model_dump"):
        return json.dumps(obj.model_dump(), indent=2, default=str)
    return json.dumps(obj, indent=2, default=str)


def _handle_tool(name: str, args: dict) -> str:
    """Dispatch a tool call to the appropriate service function."""
    # Skills
    if name == "skills_list":
        tag_list = [t.strip() for t in args["tags"].split(",")] if args.get("tags") else None
        return _serialize(skills.list_skills(type_filter=args.get("type"), tags=tag_list))
    if name == "skills_get":
        return _serialize(skills.get_skill(args["path"]))
    if name == "skills_search":
        return _serialize(skills.search_skills(args["query"]))
    if name == "skills_reindex":
        content = skills.regenerate_index()
        return json.dumps({"message": "Index regenerated", "lines": len(content.splitlines())})

    # Sessions — registry dispatch
    result = dispatch_operation(name, args, SESSION_OPERATIONS)
    if result is not None:
        return _serialize(result)

    # Tasks
    if name == "task_create":
        req = TaskCreateRequest(project=args["project"], task=args["task"],
                                from_branch=args.get("from_branch"),
                                context=args.get("context"))
        return _serialize(tasks.create_task(req))
    if name == "task_list":
        return _serialize(tasks.list_tasks(status=args.get("status"), project=args.get("project")))
    if name == "task_complete":
        req = TaskUpdateRequest(action="complete", merge=args.get("merge", False), cleanup=args.get("cleanup", False))
        return _serialize(tasks.update_task(args["project"], args["task"], req))
    if name == "task_delete":
        return _serialize(tasks.delete_task(args["project"], args["task"]))

    # Projects
    if name == "project_create":
        req = ProjectCreateRequest(**args)
        return _serialize(projects.create_project(req))

    # Locks
    if name == "lock_acquire":
        req = LockAcquireRequest(**args)
        return _serialize(locks.acquire(req))
    if name == "lock_release":
        return _serialize(locks.release(args["resource_path"], args["holder_id"]))
    if name == "lock_list":
        return _serialize(locks.list_locks(holder_id=args.get("holder_id"), path=args.get("path")))
    if name == "lock_heartbeat":
        return _serialize(locks.heartbeat(args["holder_id"]))

    # Messaging
    if name == "inbox_send":
        req = MessageSendRequest(**args)
        return _serialize(messaging.send_message(req))
    if name == "inbox_search":
        return _serialize(messaging.search_messages(
            scope=args.get("scope"), status=args.get("status"),
            msg_type=args.get("msg_type"), query=args.get("query"),
            limit=args.get("limit", 50),
        ))
    if name == "inbox_read":
        return _serialize(messaging.mark_read(args["id"]))
    if name == "inbox_recipients":
        recipients = messaging.list_recipients()
        return json.dumps({"recipients": recipients, "total": len(recipients)}, indent=2)

    # Agent spawning
    if name == "agent_spawn":
        req = AgentSpawnRequest(**args)
        return _serialize(agents.spawn_agent(req))

    # Status
    if name == "awm_status":
        from awm.config import WORKSPACE_ROOT
        from awm.db import get_connection
        conn = get_connection()
        try:
            active_locks = conn.execute("SELECT COUNT(*) FROM locks").fetchone()[0]
            active_edits = conn.execute(
                "SELECT COUNT(*) FROM shared_edits WHERE status = 'active'"
            ).fetchone()[0]
        finally:
            conn.close()
        task_result = tasks.list_tasks(status="active")
        return json.dumps({
            "status": "ok",
            "workspace_root": str(WORKSPACE_ROOT),
            "active_locks": active_locks,
            "active_tasks": task_result.total,
            "active_shared_edits": active_edits,
        }, indent=2)

    raise ValueError(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# MCP protocol handlers
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        result = _handle_tool(name, arguments)
        return [TextContent(type="text", text=result)]
    except FileNotFoundError as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
    except FileExistsError as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
    except (RuntimeError, ValueError) as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _run():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main():
    init_db()
    locks.reap_stale()
    asyncio.run(_run())


if __name__ == "__main__":
    main()
