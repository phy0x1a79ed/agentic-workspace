"""Artifact operation definitions for the registry."""

from awm.models import ArtifactRegisterRequest
from awm.registry import (
    Column,
    JsonOutput,
    Operation,
    Param,
    TableOutput,
)
from awm.services import artifacts

ARTIFACT_OPERATIONS: list[Operation] = [
    Operation(
        name="artifact_register",
        description="Register an output artifact (figure, dataset, report, etc.).",
        service_func=artifacts.register_artifact,
        http_method="POST",
        http_path="/artifacts",
        cli_group="artifact",
        cli_command="register",
        output=JsonOutput(),
        request_model=ArtifactRegisterRequest,
        params=[
            Param(name="project", type="string", required=True, description="Project name", cli_type="argument"),
            Param(name="scope", type="string", required=True, description="Scope name", cli_type="argument"),
            Param(name="name", type="string", required=True, description="Human-readable name", cli_name="--name"),
            Param(name="artifact_type", type="string", required=True, description="figure, dataset, report, model, script, other", cli_name="--type"),
            Param(name="path", type="string", required=True, description="Path relative to workspace root", cli_name="--path"),
            Param(name="description", type="string", description="What it contains", cli_name="--description"),
            Param(name="format", type="string", description="File format (svg, csv, parquet, etc.)", cli_name="--format"),
            Param(name="tags", type="string", description="Comma-separated tags", cli_name="--tags"),
        ],
    ),
    Operation(
        name="artifact_search",
        description="Search/list registered artifacts by project, type, or free-text query.",
        service_func=artifacts.search_artifacts,
        http_method="GET",
        http_path="/artifacts",
        cli_group="artifact",
        cli_command="search",
        output=TableOutput(
            list_key="artifacts",
            columns=[
                Column(key="id", header="ID", width=6),
                Column(key="project", header="PROJECT", width=15),
                Column(key="scope", header="SCOPE", width=18),
                Column(key="name", header="NAME", width=25),
                Column(key="artifact_type", header="TYPE", width=10),
                Column(key="path", header="PATH", width=40, max_len=40),
            ],
        ),
        params=[
            Param(name="project", type="string", description="Filter by project", cli_name="--project"),
            Param(name="scope", type="string", description="Filter by scope", cli_name="--scope"),
            Param(name="artifact_type", type="string", description="Filter by type", cli_name="--type"),
            Param(name="query", type="string", description="Free-text search", cli_name="--query"),
            Param(name="limit", type="integer", default=50, description="Max entries", cli_name="--limit"),
        ],
    ),
]
