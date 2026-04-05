"""Experience operation definitions for the registry."""

from awm.models import ExperienceLogRequest
from awm.registry import (
    Column,
    JsonOutput,
    Operation,
    Param,
    TableOutput,
)
from awm.services import experiences

EXPERIENCE_OPERATIONS: list[Operation] = [
    Operation(
        name="experience_log",
        description="Log an experience (execution trace), optionally attached to a skill.",
        service_func=experiences.log_experience,
        http_method="POST",
        http_path="/experiences",
        cli_group="experience",
        cli_command="log",
        output=JsonOutput(),
        request_model=ExperienceLogRequest,
        params=[
            Param(name="project", type="string", required=True, description="Project name", cli_type="argument"),
            Param(name="scope", type="string", required=True, description="Scope name", cli_type="argument"),
            Param(name="summary", type="string", required=True, description="What happened", cli_name="--summary"),
            Param(name="skill_path", type="string", description="Skill that was followed", cli_name="--skill"),
            Param(name="outcome", type="string", description="success, partial_success, failure, abandoned", cli_name="--outcome"),
            Param(name="deviations", type="string", description="What differed from the protocol", cli_name="--deviations"),
            Param(name="suggestions", type="string", description="Improvements for the skill", cli_name="--suggestions"),
            Param(name="agent_id", type="string", default="unknown", description="Agent identifier", cli_name="--agent"),
        ],
    ),
    Operation(
        name="experience_list",
        description="List experiences, optionally filtered by skill, project, or scope.",
        service_func=experiences.list_experiences,
        http_method="GET",
        http_path="/experiences",
        cli_group="experience",
        cli_command="list",
        output=TableOutput(
            list_key="experiences",
            columns=[
                Column(key="id", header="ID", width=6),
                Column(key="project", header="PROJECT", width=15),
                Column(key="scope", header="SCOPE", width=20),
                Column(key="skill_path", header="SKILL", width=25),
                Column(key="outcome", header="OUTCOME", width=16),
                Column(key="summary", header="SUMMARY", width=40, max_len=40),
            ],
        ),
        params=[
            Param(name="skill_path", type="string", description="Filter by skill path", cli_name="--skill"),
            Param(name="project", type="string", description="Filter by project", cli_name="--project"),
            Param(name="scope", type="string", description="Filter by scope", cli_name="--scope"),
            Param(name="limit", type="integer", default=50, description="Max entries", cli_name="--limit"),
        ],
    ),
]
