"""Tool definitions and dispatch logic shared by the MCP proxy and the HTTP core.

This module is imported by both:

- ``awm.mcp_server`` — the thin stdio MCP client, which uses ``TOOL_DEFINITIONS``
  to respond to ``list_tools`` and forwards calls over HTTP.
- ``awm.server`` — the FastAPI core, whose ``POST /invoke`` endpoint calls
  ``handle_tool`` directly so the logic lives in one place.

Keeping both in the same module means the MCP schemas and the dispatch body
cannot drift. If/when operations migrate to the ``awm.registry`` framework the
``handle_tool`` body can be replaced with a single ``dispatch_operation`` call.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.types import Tool

from awm.models import (
    ArtifactRegisterRequest,
    LockAcquireRequest,
    MessageSendRequest,
    ProjectCreateRequest,
    ScopeCreateRequest,
    ScopeUpdateRequest,
    ScopeSyncRequest,
)
from awm.operations.sessions import SESSION_OPERATIONS
from awm._lib.operations import dispatch_operation, operations_to_mcp_tools
from awm.services import artifacts, core, locks, messaging, projects, scopes, skills


# ---------------------------------------------------------------------------
# Tool definitions (MCP schemas)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[Tool] = [
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
        name="skills_sync",
        description="Lazily reconcile the skills embedding index with the live skills directory. Cheap no-op when nothing has changed; re-embeds and prunes on drift.",
        inputSchema={
            "type": "object",
            "properties": {
                "force": {"type": "boolean", "description": "Bypass the fingerprint cache and sync unconditionally", "default": False},
            },
        },
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
    Tool(
        name="scope_sync",
        description="Sync a scope's feature branch with its base branch (merge by default, or rebase). Refuses if the worktree is dirty. Does not fetch — pull the base branch first if upstream commits are wanted.",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "scope": {"type": "string"},
                "strategy": {"type": "string", "enum": ["merge", "rebase"], "default": "merge"},
                "from_branch": {"type": "string", "description": "Base branch (default: project's default branch)"},
            },
            "required": ["project", "scope"],
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
    Tool(
        name="artifact_delete",
        description="Permanently delete an artifact by ID. Use artifact_search first to find the ID.",
        inputSchema={
            "type": "object",
            "properties": {
                "artifact_id": {"type": "integer", "description": "Artifact ID (from artifact_search results)"},
            },
            "required": ["artifact_id"],
        },
    ),
    Tool(
        name="artifacts_sync",
        description="Lazily reconcile artifact status and embeddings with on-disk file presence. Cheap no-op when the artifacts DB is unchanged; on drift, flips missing files to 'stale', restores reappeared ones, and prunes embeddings.",
        inputSchema={
            "type": "object",
            "properties": {
                "force": {"type": "boolean", "description": "Bypass the DB fingerprint and walk every artifact path", "default": False},
            },
        },
    ),
    # AWM Refresh
    Tool(
        name="awm_refresh",
        description="Regenerate .awm/history.md and .awm/artifacts.md for a scope from current DB state.",
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
        description=(
            "Browse message PREVIEWS (no body/metadata) across scopes. Use for "
            "triage and discovery. To read full message bodies for a specific "
            "scope, use `inbox_fetch`."
        ),
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
        name="inbox_fetch",
        description=(
            "Retrieve full messages (body + metadata) for a scope. This is the "
            "'read my inbox' primitive. Set `mark_read=true` to atomically mark "
            "the returned messages as read in the same call."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "scope": {"type": "string", "description": "Target scope: 'workspace', 'project:X', or 'scope:X/Y'"},
                "status": {"type": "string", "description": "Filter by status: unread or read"},
                "msg_type": {"type": "string", "description": "Filter by message type"},
                "limit": {"type": "integer", "default": 50},
                "mark_read": {"type": "boolean", "default": False, "description": "If true, mark any returned unread messages as read atomically"},
            },
            "required": ["scope"],
        },
    ),
    Tool(
        name="inbox_mark_read",
        description=(
            "Mark a single message as read by ID. For bulk consumption of a "
            "scope's unread messages, prefer `inbox_fetch` with `mark_read=true`."
        ),
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
        description=(
            "List valid recipient scopes (workspace + projects + scopes) "
            "matching a regex. Pass query='*' to return all recipients."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Regex matched against recipient strings, or '*' for all.",
                },
                "peer": {
                    "type": "string",
                    "description": "Reserved for future peer fan-out; only 'local' / omitted is honoured today.",
                },
            },
            "required": ["query"],
        },
    ),
    # `agent_spawn` was removed when rooms became the orchestration primitive.
    # Use `room_create --scope ... --prompt ...` (M5) instead.
    Tool(
        name="room_create",
        description="Create a multi-participant room; optionally seed with agent scopes and per-scope prompts.",
        inputSchema={
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "scopes": {"type": "array", "items": {"type": "string"},
                            "description": "List of project/scope[@peer] identifiers to enroll as agents"},
                "prompts": {"type": "object",
                             "description": "Per-scope opener prompts, keyed by scope identifier"},
                "close_on_exit": {"type": "boolean"},
            },
        },
    ),
    Tool(
        name="room_list",
        description="List active rooms (optionally fan out across peers).",
        inputSchema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "active|closed|all"},
                "participating_scope": {"type": "string"},
                "peer": {"type": "string", "description": "all|<peer-id> for fan-out"},
                "limit": {"type": "integer"},
            },
        },
    ),
    Tool(
        name="room_get",
        description="Get a room's metadata, participants, and recent transcript.",
        inputSchema={
            "type": "object",
            "properties": {"room_id": {"type": "string"}},
            "required": ["room_id"],
        },
    ),
    Tool(
        name="room_history",
        description="Fetch a longer slice of a room's transcript (by char-budget).",
        inputSchema={
            "type": "object",
            "properties": {
                "room_id": {"type": "string"},
                "limit_chars": {"type": "integer"},
                "before_ts": {"type": "string"},
            },
            "required": ["room_id"],
        },
    ),
    Tool(
        name="room_search",
        description="Search rooms by topic, id, or transcript content (with peer fan-out).",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "peer": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="room_post",
        description=(
            "Post a message into a room. Agents that participate receive it as a framed stdin input. "
            "Posts starting with '/' and addressed via 'to:' are intercepted server-side as agent-control "
            "commands — prefer the agent_control tool for those."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "room_id": {"type": "string"},
                "body": {"type": "string"},
                "kind": {"type": "string", "description": "text|tool_use|tool_result|system (default: text)"},
                "to": {"type": "string", "description": "Optional scope to direct-address"},
            },
            "required": ["room_id", "body"],
        },
    ),
    Tool(
        name="room_invite",
        description="Invite a scope (agent) into a room. Spawns the live session if not already running.",
        inputSchema={
            "type": "object",
            "properties": {
                "room_id": {"type": "string"},
                "scope": {"type": "string", "description": "project/scope[@peer]"},
                "prompt": {"type": "string"},
            },
            "required": ["room_id", "scope"],
        },
    ),
    Tool(
        name="room_remove",
        description="Remove a scope from a room (the agent process keeps running).",
        inputSchema={
            "type": "object",
            "properties": {
                "room_id": {"type": "string"},
                "scope": {"type": "string"},
            },
            "required": ["room_id", "scope"],
        },
    ),
    Tool(
        name="room_close",
        description="Close a room; optionally SIGTERM every participating local agent.",
        inputSchema={
            "type": "object",
            "properties": {
                "room_id": {"type": "string"},
                "kill_agents": {"type": "boolean"},
            },
            "required": ["room_id"],
        },
    ),
    Tool(
        name="room_archive",
        description="Soft-archive a room; refused if it still has active scope participants.",
        inputSchema={
            "type": "object",
            "properties": {"room_id": {"type": "string"}},
            "required": ["room_id"],
        },
    ),
    Tool(
        name="room_agents",
        description="List a room's scope/shadow_peer participants with attached live agent state.",
        inputSchema={
            "type": "object",
            "properties": {"room_id": {"type": "string"}},
            "required": ["room_id"],
        },
    ),
    Tool(
        name="agent_control",
        description=(
            "Send a harness control command to a live agent participating in a room. "
            "Wraps the server slash-command catalog: /compact, /clear, /restart, /yolo, /plan, "
            "/mode <mode>, /model <name>, /effort <level>, /kill, /help. "
            "Call with command='/help' to list commands and their args for that scope. "
            "The result is also posted into the room as a system message for audit. "
            "Example: agent_control(room_id='r-abc', scope='awm/dev', command='/yolo')."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "room_id": {"type": "string", "description": "Room containing the target scope as a participant"},
                "scope": {"type": "string", "description": "Target agent scope, e.g. 'awm/dev'"},
                "command": {"type": "string", "description": "Slash command line, e.g. '/compact' or '/mode plan'"},
            },
            "required": ["room_id", "scope", "command"],
        },
    ),
    # Peers (control-center)
    Tool(
        name="peers_list",
        description="List registered remote awm peers.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="peer_ping",
        description="Probe a registered peer via its SSH tunnel; reports reachability and round-trip ms.",
        inputSchema={
            "type": "object",
            "properties": {"peer_id": {"type": "string"}},
            "required": ["peer_id"],
        },
    ),
    Tool(
        name="project_list",
        description="List projects derived from the scopes table, with per-status scope counts.",
        inputSchema={"type": "object", "properties": {}},
    ),
    # Restart
    Tool(
        name="awm_restart",
        description=(
            "Restart AWM systemd units. ``target='core'`` restarts only the "
            "core daemon (default historical behavior); ``'exposed'`` "
            "restarts only awm-exposed; ``'all'`` (default) restarts both."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "enum": ["core", "exposed", "all"],
                    "description": "Which unit(s) to restart.",
                    "default": "all",
                },
            },
        },
    ),
    # Status
    Tool(
        name="awm_status",
        description="Get AWM server status: workspace root, active locks, scopes, shared edits, and core process info.",
        inputSchema={"type": "object", "properties": {}},
    ),
    # MCP-config fan-out
    Tool(
        name="awm_mcp_sync",
        description="Read workspace .mcp.json and regenerate backend-specific MCP configs (claude-spawn, opencode) under .awm/.",
        inputSchema={"type": "object", "properties": {}},
    ),
]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _serialize(obj: Any) -> str:
    """Serialize a Pydantic model (or dict) to JSON string."""
    if hasattr(obj, "model_dump"):
        return json.dumps(obj.model_dump(), indent=2, default=str)
    return json.dumps(obj, indent=2, default=str)


def _dispatch_room_tool(name: str, args: dict) -> str:
    """Dispatch a ``room_*`` / ``agent_control`` MCP tool in-process.

    Routes go through the same services modules the FastAPI router uses
    (``rooms_svc``, ``orchestration``, ``agent_slash``, federation), so
    the MCP surface stays consistent with the HTTP surface even when no
    daemon is reachable. Closes inbox #160/#161.
    """
    from awm.services import (
        agent_slash as agent_slash_svc,
        orchestration,
        rooms as rooms_svc,
    )
    from awm.services.network import federation, peers as peer_svc

    author = args.get("author") or "user:operator"

    def _peer_ids(spec: str) -> list[str]:
        if spec == "all":
            return [p["peer_id"] for p in peer_svc.list_peers()]
        return [spec]

    if name == "room_create":
        # orchestration.create_room_with_scopes is async; run it on a
        # fresh loop because handle_tool is called from sync MCP handlers.
        room = asyncio.run(
            orchestration.create_room_with_scopes(
                topic=args.get("topic"),
                scopes=args.get("scopes", []) or [],
                prompts=args.get("prompts", {}) or {},
                opener=author,
                close_on_exit=bool(args.get("close_on_exit", False)),
            )
        )
        return json.dumps(room.to_dict(), indent=2, default=str)

    if name == "room_list":
        status = args.get("status", "active")
        participating_scope = args.get("participating_scope")
        limit = int(args.get("limit", 100))
        local_rows = [
            r.to_dict() for r in rooms_svc.list_rooms(
                status=status,
                participating_scope=participating_scope,
                limit=limit,
            )
        ]
        merged = list(local_rows)
        if (peer := args.get("peer")) and peer != "local":
            for pid in _peer_ids(peer):
                try:
                    remote = federation.forward_room_list(
                        pid, status=status,
                        participating_scope=participating_scope, limit=limit,
                    )
                    merged.extend(remote.get("rooms", []))
                except federation.FederationError:
                    continue
        return json.dumps({"rooms": merged, "total": len(merged)},
                          indent=2, default=str)

    if name == "room_get":
        room = rooms_svc.get_room(args["room_id"])
        if room is None:
            raise RuntimeError(f"no such room: {args['room_id']}")
        participants = rooms_svc.list_participants(args["room_id"], active_only=False)
        recent = rooms_svc.history(args["room_id"], limit_chars=1024)
        return json.dumps({
            "room": room.to_dict(),
            "participants": [p.to_dict() for p in participants],
            "recent": [p.to_dict() for p in recent],
        }, indent=2, default=str)

    if name == "room_history":
        limit_chars = int(args.get("limit_chars", 4096))
        before_ts = args.get("before_ts")
        try:
            posts = rooms_svc.history(
                args["room_id"], limit_chars=limit_chars, before_ts=before_ts,
            )
        except rooms_svc.RoomNotFound:
            raise RuntimeError(f"no such room: {args['room_id']}")
        return json.dumps(
            {"posts": [p.to_dict() for p in posts], "total": len(posts)},
            indent=2, default=str,
        )

    if name == "room_search":
        query = args["query"]
        limit = int(args.get("limit", 20))
        local_hits = [r.to_dict() for r in rooms_svc.search_rooms(query, limit=limit)]
        degraded: list[dict] = []
        if (peer := args.get("peer")) and peer != "local":
            for pid in _peer_ids(peer):
                try:
                    remote = federation.forward_room_search(pid, query, limit=limit)
                    local_hits.extend(remote.get("rooms", []))
                except federation.FederationError as exc:
                    degraded.append({"peer_id": pid, "reason": str(exc)})
        return json.dumps(
            {"rooms": local_hits, "total": len(local_hits), "degraded": degraded},
            indent=2, default=str,
        )

    if name == "room_post":
        try:
            post = rooms_svc.post(
                args["room_id"], author=author, body=args["body"],
                kind=args.get("kind", "text"), to_scope=args.get("to"),
            )
        except rooms_svc.RoomNotFound:
            raise RuntimeError(f"no such room: {args['room_id']}")
        except rooms_svc.RoomClosed:
            raise RuntimeError(f"room {args['room_id']} is closed")
        return json.dumps({"message": "posted", "post": post.to_dict()},
                          indent=2, default=str)

    if name == "room_invite":
        try:
            participant = asyncio.run(
                orchestration.invite_scope_to_room(
                    args["room_id"], args["scope"],
                    prompt=args.get("prompt"), opener=author,
                )
            )
        except rooms_svc.RoomNotFound:
            raise RuntimeError(f"no such room: {args['room_id']}")
        except rooms_svc.RoomClosed:
            raise RuntimeError(f"room {args['room_id']} is closed")
        except rooms_svc.ScopeBusyError:
            participant = rooms_svc.add_participant(
                args["room_id"], "scope", args["scope"],
            )
        return json.dumps(
            {"message": "invited", "participant": participant.to_dict()},
            indent=2, default=str,
        )

    if name == "room_remove":
        ok = rooms_svc.remove_participant(args["room_id"], "scope", args["scope"])
        if not ok:
            raise RuntimeError("participant not found or already left")
        return json.dumps({"message": "removed"}, indent=2)

    if name == "room_close":
        try:
            room = rooms_svc.close_room(
                args["room_id"], kill_agents=bool(args.get("kill_agents", False)),
            )
        except rooms_svc.RoomNotFound:
            raise RuntimeError(f"no such room: {args['room_id']}")
        return json.dumps({"message": "closed", "room": room.to_dict()},
                          indent=2, default=str)

    if name == "room_archive":
        try:
            room = rooms_svc.archive_room(args["room_id"])
        except rooms_svc.RoomNotFound:
            raise RuntimeError(f"no such room: {args['room_id']}")
        except rooms_svc.RoomArchiveBlocked as exc:
            raise RuntimeError(
                f"room_archive_blocked: room={exc.room_id} "
                f"blocking_scopes={exc.blocking_scopes}"
            )
        return json.dumps({"message": "archived", "room": room.to_dict()},
                          indent=2, default=str)

    if name == "room_agents":
        from awm.services import agent_instances
        room = rooms_svc.get_room(args["room_id"])
        if room is None:
            raise RuntimeError(f"no such room: {args['room_id']}")
        agents: list[dict] = []
        for p in rooms_svc.list_participants(args["room_id"], active_only=False):
            if p.kind not in ("scope", "shadow_peer"):
                continue
            live: dict | None = None
            if p.kind == "scope":
                base_scope, peer = rooms_svc._split_scope(p.identifier)
                if peer is None:
                    sess = agent_instances._by_scope.get(base_scope)
                    if sess is not None:
                        live = {
                            "pid": getattr(sess.proc, "pid", None),
                            "status": sess.status,
                            "started_at": sess.started_at,
                            "exited_at": sess.exited_at,
                            "exit_code": sess.exit_code,
                            "agent_cli": sess.agent_cli,
                            "permission_mode": sess.permission_mode,
                            "model": sess.model,
                            "effort": sess.effort,
                            "claude_session_id": sess.claude_session_id,
                            "context_used": sess.context_used,
                            "context_max": sess.context_max,
                        }
            agents.append({
                "scope": p.identifier, "kind": p.kind,
                "identifier": p.identifier, "joined_at": p.joined_at,
                "live": live,
            })
        return json.dumps({"agents": agents}, indent=2, default=str)

    if name == "agent_control":
        room_id = args["room_id"]
        scope = args["scope"]
        line = (args.get("command") or "").strip()
        if not line.startswith("/"):
            raise RuntimeError("slash commands must start with '/'")
        try:
            rooms_svc.post(room_id, author=author, body=line, kind="slash",
                           to_scope=scope)
        except rooms_svc.RoomError as exc:
            raise RuntimeError(str(exc))
        try:
            handled, message = asyncio.run(agent_slash_svc.dispatch(scope, line))
        except agent_slash_svc.SlashParseError as exc:
            raise RuntimeError(str(exc))
        if handled:
            try:
                rooms_svc.post(room_id, author="system", body=message, kind="system")
            except rooms_svc.RoomError:
                pass
            return json.dumps({"handled": True, "result": message}, indent=2)
        # Fall through to claude stdin for unknown slash commands.
        from awm.services import agent_instances
        try:
            asyncio.run(agent_instances.send_slash(scope, line))
        except agent_instances.NoSessionError as exc:
            raise RuntimeError(str(exc))
        return json.dumps({"handled": False, "result": ""}, indent=2)

    raise RuntimeError(f"unhandled room tool: {name}")


def handle_tool(name: str, args: dict) -> str:
    """Dispatch a tool call to the appropriate service function.

    Returns a JSON string. Raises ``ValueError`` for unknown tools; other
    exceptions propagate for the caller (MCP proxy or FastAPI handler) to
    translate into protocol-appropriate errors.
    """
    # Skills
    if name == "skills_list":
        tag_list = [t.strip() for t in args["tags"].split(",")] if args.get("tags") else None
        return _serialize(skills.list_skills(type_filter=args.get("type"), tags=tag_list))
    if name == "skills_get":
        return _serialize(skills.get_skill(args["path"]))
    if name == "skills_search":
        return _serialize(skills.search_skills(args["query"]))
    if name == "skills_sync":
        return json.dumps(skills.sync_skills(force=args.get("force", False)))

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
    if name == "scope_sync":
        req = ScopeSyncRequest(
            strategy=args.get("strategy", "merge"),
            from_branch=args.get("from_branch"),
        )
        return _serialize(scopes.sync_scope(args["project"], args["scope"], req))

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
    if name == "artifact_delete":
        return json.dumps(artifacts.delete_artifact(args["artifact_id"]))
    if name == "artifacts_sync":
        return json.dumps(artifacts.sync_artifacts(force=args.get("force", False)))

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
    if name == "inbox_fetch":
        return _serialize(messaging.fetch_messages(
            scope=args["scope"], status=args.get("status"),
            msg_type=args.get("msg_type"), limit=args.get("limit", 50),
            mark_read=args.get("mark_read", False),
        ))
    if name == "inbox_mark_read":
        return _serialize(messaging.mark_read(args["id"]))
    if name == "inbox_recipients":
        recipients = messaging.list_recipients(
            query=args["query"], peer=args.get("peer"),
        )
        return json.dumps(
            {"recipients": recipients, "total": len(recipients), "query": args["query"]},
            indent=2,
        )

    # Rooms — dispatch through the local exposed app for consistent auth
    # + cross-peer fan-out semantics.
    if name in (
        "room_create", "room_list", "room_get", "room_history",
        "room_search", "room_post", "room_invite", "room_remove",
        "room_close", "room_archive", "room_agents", "agent_control",
    ):
        return _dispatch_room_tool(name, args)

    # Peers (control-center surface)
    if name == "peers_list":
        from awm.services.network import peers as peer_svc
        rows = peer_svc.list_peers()
        return json.dumps({"peers": rows, "total": len(rows)}, indent=2, default=str)
    if name == "peer_ping":
        import time as _time
        from awm.services.network import peers as peer_svc
        t0 = _time.monotonic()
        result = peer_svc.ping_peer(args["peer_id"])
        rtt_ms = (_time.monotonic() - t0) * 1000.0
        ok = bool(result.get("ok"))
        return json.dumps({
            "peer_id": args["peer_id"],
            "ok": ok,
            "rtt_ms": round(rtt_ms, 2) if ok else None,
            "error": None if ok else (result.get("reason") or "ping failed"),
        }, indent=2)
    if name == "project_list":
        from awm.db import get_connection
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT project, status, COUNT(*) AS n FROM scopes "
                "GROUP BY project, status ORDER BY project"
            ).fetchall()
        finally:
            conn.close()
        by_project: dict = {}
        for r in rows:
            counts = by_project.setdefault(r["project"], {"active": 0, "completed": 0, "deleted": 0})
            if r["status"] in counts:
                counts[r["status"]] = r["n"]
        return json.dumps({
            "projects": [{"name": n, "scope_counts": c} for n, c in by_project.items()],
        }, indent=2)

    # Restart
    if name == "awm_restart":
        target = args.get("target", "all")
        return _serialize(core.restart_core(target=target))

    # Status
    if name == "awm_status":
        return _awm_status()

    # MCP-config fan-out
    if name == "awm_mcp_sync":
        from awm.exports import sync_mcp_configs
        return _serialize(sync_mcp_configs())

    raise ValueError(f"Unknown tool: {name}")


def _awm_status() -> str:
    """Return awm core status including process/env info for drift detection."""
    import os
    import sys
    import time

    from awm import config
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

    # Core process info (populated when dispatched in-process on the core).
    core_start = getattr(_awm_status, "_core_start_time", None)
    uptime = int(time.time() - core_start) if core_start else None

    # Exposed-process info (separate systemd unit; check pid file + liveness).
    exposed_pid: int | None = None
    exposed_alive = False
    try:
        if config.EXPOSED_PID_FILE.exists():
            exposed_pid = int(config.EXPOSED_PID_FILE.read_text().strip() or "0") or None
            if exposed_pid:
                try:
                    os.kill(exposed_pid, 0)
                    exposed_alive = True
                except (ProcessLookupError, PermissionError):
                    exposed_alive = False
    except (OSError, ValueError):
        exposed_pid = None

    exposed_info: dict[str, Any] = {
        "pid": exposed_pid,
        "alive": exposed_alive,
        "port": getattr(config, "EXPOSED_PORT", None),
        "scheme": "https" if getattr(config, "TLS_CERT", None) and config.TLS_CERT.exists() else "http",
    }

    return json.dumps({
        "status": "ok",
        "workspace_root": str(WORKSPACE_ROOT),
        "active_locks": active_locks,
        "active_scopes": scope_result.total,
        "active_shared_edits": active_edits,
        "core_pid": os.getpid(),
        "core_uptime_s": uptime,
        "core_workspace_root": os.environ.get("AWM_WORKSPACE"),
        "core_python": sys.executable,
        "core_sys_path_head": sys.path[:3],
        "exposed": exposed_info,
    }, indent=2)


def mark_core_start() -> None:
    """Record the core process start time for uptime reporting."""
    import time
    _awm_status._core_start_time = time.time()  # type: ignore[attr-defined]
