"""MCP stdio server for the Agentic Workspace Manager.

Exposes AWM services as MCP tools so Claude Code (and other MCP clients)
can manage projects, scopes, locks, skills, experiences, artifacts, and sessions.
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
    ArtifactRegisterRequest,
    ExperienceLogRequest,
    LockAcquireRequest,
    MessageSendRequest,
    ProjectCreateRequest,
    ScopeCreateRequest,
    ScopeUpdateRequest,
)
from awm.operations.sessions import SESSION_OPERATIONS
from awm.registry import dispatch_operation, operations_to_mcp_tools
from awm.services import agents, artifacts, experiences, locks, messaging, projects, scopes, skills

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
                "type": {"type": "string", "description": "Filter by skill type (protocol, reference, template)"},
                "tags": {"type": "string", "description": "Comma-separated tags to filter by"},
            },
        },
    ),
    Tool(
        name="skills_get",
        description="Read a skill file by relative path (e.g. 'tools/git.md').",
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
    # Scopes (formerly Tasks)
    Tool(
        name="scope_create",
        description="Create a new scope (worktree + .awm/ metadata) for a project.",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "scope": {"type": "string"},
                "from_branch": {"type": "string"},
                "context": {"type": "string", "description": "Seed content for .awm/context.md"},
            },
            "required": ["project", "scope"],
        },
    ),
    Tool(
        name="scope_list",
        description="List scopes, optionally filtered by status and/or project.",
        inputSchema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "active, completed, deleted, or all"},
                "project": {"type": "string"},
            },
        },
    ),
    Tool(
        name="scope_complete",
        description="Complete a scope, optionally merging the feature branch and/or cleaning up the worktree.",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "scope": {"type": "string"},
                "merge": {"type": "boolean", "default": False},
                "cleanup": {"type": "boolean", "default": False, "description": "Remove worktree and branch after completion"},
            },
            "required": ["project", "scope"],
        },
    ),
    Tool(
        name="scope_delete",
        description="Delete a scope — clean up its worktree, branch, and mark as deleted in DB.",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "scope": {"type": "string"},
            },
            "required": ["project", "scope"],
        },
    ),
    # Experiences
    Tool(
        name="experience_log",
        description="Log an experience (execution trace), optionally attached to a skill. Auto-captures skill git version.",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "scope": {"type": "string"},
                "skill_path": {"type": "string", "description": "Skill that was followed (optional)"},
                "outcome": {"type": "string", "enum": ["success", "partial_success", "failure", "abandoned"]},
                "summary": {"type": "string", "description": "What happened"},
                "deviations": {"type": "string", "description": "What differed from the protocol"},
                "suggestions": {"type": "string", "description": "Improvements for the skill"},
                "agent_id": {"type": "string", "default": "unknown"},
            },
            "required": ["project", "scope", "summary"],
        },
    ),
    Tool(
        name="experience_list",
        description="List experiences, optionally filtered by skill, project, or scope.",
        inputSchema={
            "type": "object",
            "properties": {
                "skill_path": {"type": "string", "description": "Filter by skill path"},
                "project": {"type": "string"},
                "scope": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
            },
        },
    ),
    # Artifacts
    Tool(
        name="artifact_register",
        description="Register an output artifact (figure, dataset, report, etc.).",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "scope": {"type": "string"},
                "name": {"type": "string", "description": "Human-readable name"},
                "artifact_type": {"type": "string", "enum": ["figure", "dataset", "report", "model", "script", "other"]},
                "path": {"type": "string", "description": "Path relative to workspace root"},
                "description": {"type": "string"},
                "format": {"type": "string", "description": "File format (svg, csv, parquet, etc.)"},
                "tags": {"type": "string", "description": "Comma-separated tags"},
            },
            "required": ["project", "scope", "name", "artifact_type", "path"],
        },
    ),
    Tool(
        name="artifact_search",
        description="Search/list registered artifacts by project, type, or free-text query.",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "scope": {"type": "string"},
                "artifact_type": {"type": "string"},
                "query": {"type": "string", "description": "Free-text search across name, description, tags"},
                "limit": {"type": "integer", "default": 50},
            },
        },
    ),
    # AWM Refresh
    Tool(
        name="awm_refresh",
        description="Regenerate .awm/knowledge.md and .awm/artifacts.md for a scope from current DB state.",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "scope": {"type": "string"},
            },
            "required": ["project", "scope"],
        },
    ),
    # Projects
    Tool(
        name="project_create",
        description="Create a new project with bare repository, worktree, and data directories.",
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
        description="Send a message to a scoped inbox (workspace, project:X, or scope:X/Y).",
        inputSchema={
            "type": "object",
            "properties": {
                "scope": {"type": "string", "description": "Target scope: 'workspace', 'project:X', or 'scope:X/Y'"},
                "sender": {"type": "string", "description": "Sender identifier (agent name or scope)"},
                "msg_type": {"type": "string", "enum": ["scope_assignment", "reflection", "status_update", "notification", "plan"]},
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
        description="Read messages from a scoped inbox. Returns messages and marks unread ones as read.",
        inputSchema={
            "type": "object",
            "properties": {
                "scope": {"type": "string", "description": "Inbox scope: 'workspace', 'project:X', or 'task:X/Y'"},
                "status": {"type": "string", "description": "Filter by status: 'unread' (default), 'read', or omit for unread only"},
                "limit": {"type": "integer", "default": 50},
            },
            "required": ["scope"],
        },
    ),
    Tool(
        name="inbox_recipients",
        description="List valid recipient scopes (workspace + all projects + all scopes).",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="agent_spawn",
        description="Spawn a fire-and-forget agent subprocess on a scope workspace.",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "scope": {"type": "string"},
                "prompt": {"type": "string", "description": "Optional prompt/plan for the agent (sent to scope inbox)"},
                "agent_cli": {"type": "string", "description": "CLI to use: 'opencode' or 'claude' (default from config)"},
            },
            "required": ["project", "scope"],
        },
    ),
    # Status
    Tool(
        name="awm_status",
        description="Get AWM server status: workspace root, active locks, scopes, and shared edits.",
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

    # Scopes
    if name == "scope_create":
        req = ScopeCreateRequest(project=args["project"], scope=args["scope"],
                                from_branch=args.get("from_branch"),
                                context=args.get("context"))
        return _serialize(scopes.create_scope(req))
    if name == "scope_list":
        return _serialize(scopes.list_scopes(status=args.get("status"), project=args.get("project")))
    if name == "scope_complete":
        req = ScopeUpdateRequest(action="complete", merge=args.get("merge", False), cleanup=args.get("cleanup", False))
        return _serialize(scopes.update_scope(args["project"], args["scope"], req))
    if name == "scope_delete":
        return _serialize(scopes.delete_scope(args["project"], args["scope"]))

    # Experiences
    if name == "experience_log":
        req = ExperienceLogRequest(**{k: v for k, v in args.items() if v is not None})
        return _serialize(experiences.log_experience(req))
    if name == "experience_list":
        return _serialize(experiences.list_experiences(
            skill_path=args.get("skill_path"), project=args.get("project"),
            scope=args.get("scope"), limit=args.get("limit", 50),
        ))

    # Artifacts
    if name == "artifact_register":
        req = ArtifactRegisterRequest(**{k: v for k, v in args.items() if v is not None})
        return _serialize(artifacts.register_artifact(req))
    if name == "artifact_search":
        return _serialize(artifacts.search_artifacts(
            project=args.get("project"), scope=args.get("scope"),
            artifact_type=args.get("artifact_type"), query=args.get("query"),
            limit=args.get("limit", 50),
        ))

    # AWM Refresh
    if name == "awm_refresh":
        return _serialize(scopes.awm_refresh(args["project"], args["scope"]))

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
        return _serialize(messaging.read_inbox(
            scope=args["scope"],
            status=args.get("status", "unread"),
            limit=args.get("limit", 50),
        ))
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
        scope_result = scopes.list_scopes(status="active")
        return json.dumps({
            "status": "ok",
            "workspace_root": str(WORKSPACE_ROOT),
            "active_locks": active_locks,
            "active_scopes": scope_result.total,
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
