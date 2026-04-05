"""Session operation definitions for the registry."""

from awm.models import SessionLogCreateRequest
from awm.registry import (
    Column,
    DetailOutput,
    JsonOutput,
    Operation,
    Param,
    TableOutput,
)
from awm.services import sessions

SESSION_OPERATIONS: list[Operation] = [
    Operation(
        name="session_log",
        description="Log a session entry and record metadata in DB.",
        service_func=sessions.log_session,
        http_method="POST",
        http_path="/sessions",
        cli_group="session",
        cli_command="log",
        output=JsonOutput(),
        request_model=SessionLogCreateRequest,
        params=[
            Param(name="project", type="string", required=True, description="Project name", cli_type="argument"),
            Param(name="scope", type="string", required=True, description="Scope name", cli_type="argument"),
            Param(name="summary", type="string", required=True, description="Session summary", cli_name="--summary"),
            Param(name="decisions", type="array", description="Decisions made (repeatable)", cli_name="--decision"),
            Param(name="issues", type="array", description="Issues encountered (repeatable)", cli_name="--issue"),
            Param(name="next_steps", type="array", description="Next steps (repeatable)", cli_name="--next-step"),
            Param(name="agent_id", type="string", default="unknown", description="Agent identifier", cli_name="--agent"),
            Param(name="skill_path", type="string", description="Path of the skill followed this session, if any", cli_name="--skill"),
        ],
    ),
    Operation(
        name="session_list",
        description="List session log entries, optionally filtered by project and/or scope.",
        service_func=sessions.list_sessions,
        http_method="GET",
        http_path="/sessions",
        cli_group="session",
        cli_command="list",
        output=TableOutput(
            list_key="entries",
            columns=[
                Column(key="id", header="ID", width=6),
                Column(key="project", header="PROJECT", width=18),
                Column(key="scope", header="SCOPE", width=22),
                Column(key="skill_path", header="SKILL", width=20, max_len=20),
                Column(key="agent_id", header="AGENT", width=15),
                Column(key="logged_at", header="LOGGED AT", width=28),
                Column(key="summary", header="SUMMARY", width=40, max_len=40),
            ],
        ),
        params=[
            Param(name="project", type="string", description="Filter by project", cli_name="--project"),
            Param(name="scope", type="string", description="Filter by scope", cli_name="--scope"),
            Param(name="limit", type="integer", default=50, description="Max entries to return", cli_name="--limit"),
        ],
    ),
    Operation(
        name="session_get",
        description="Get a session log entry by ID with full content.",
        service_func=sessions.get_session,
        http_method="GET",
        http_path="/sessions/{session_id}",
        cli_group="session",
        cli_command="get",
        output=DetailOutput(
            fields=[
                ("ID", "entry.id"),
                ("Project", "entry.project"),
                ("Scope", "entry.scope"),
                ("Skill", "entry.skill_path"),
                ("Agent", "entry.agent_id"),
                ("Logged", "entry.logged_at"),
                ("Commit", "entry.git_commit"),
            ],
            body_field="content",
        ),
        params=[
            Param(name="session_id", type="integer", required=True, location="path", description="Session log ID", cli_type="argument"),
        ],
    ),
]
