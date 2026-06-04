"""Scope operation definitions for the registry."""

from awm.models import ScopeCreateRequest, ScopeUpdateRequest
from awm._lib.operations import (
    Column,
    JsonOutput,
    Operation,
    Param,
    TableOutput,
)
from awm.services import scopes

SCOPE_OPERATIONS: list[Operation] = [
    Operation(
        name="scope_create",
        description="Create a new scope (worktree + .awm/ metadata) for a project.",
        service_func=scopes.create_scope,
        http_method="POST",
        http_path="/scopes",
        cli_group="scope",
        cli_command="create",
        output=JsonOutput(),
        request_model=ScopeCreateRequest,
        params=[
            Param(name="project", type="string", required=True, description="Project name", cli_type="argument"),
            Param(name="scope", type="string", required=True, description="Scope name", cli_type="argument"),
            Param(name="from_branch", type="string", description="Base branch (default: main)", cli_name="--from"),
            Param(name="context", type="string", description="Seed content for .awm/context.md", cli_name="--context"),
        ],
    ),
    Operation(
        name="scope_list",
        description="List scopes, optionally filtered by status and/or project.",
        service_func=scopes.list_scopes,
        http_method="GET",
        http_path="/scopes",
        cli_group="scope",
        cli_command="list",
        output=TableOutput(
            list_key="scopes",
            columns=[
                Column(key="project", header="PROJECT", width=18),
                Column(key="scope", header="SCOPE", width=22),
                Column(key="status", header="STATUS", width=12),
                Column(key="branch", header="BRANCH", width=22),
                Column(key="session", header="SESSION", width=8),
            ],
        ),
        params=[
            Param(name="status", type="string", description="active, completed, deleted, or all", cli_name="--status"),
            Param(name="project", type="string", description="Filter by project", cli_name="--project"),
        ],
    ),
]
