"""Pydantic request/response models owned by the artifacts service."""

from __future__ import annotations

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

class ArtifactRegisterRequest(BaseModel):
    project: str
    scope: str
    name: str
    artifact_type: str = Field(
        ...,
        pattern="^(figure|dataset|report|model|script|other)$",
    )
    path: str
    description: str | None = None
    format: str | None = None
    tags: str | None = None


class ArtifactInfo(BaseModel):
    id: int
    project: str
    scope: str
    name: str
    artifact_type: str
    path: str
    description: str | None
    format: str | None
    tags: str | None
    status: str
    created_at: str


class ArtifactSearchResponse(BaseModel):
    artifacts: list[ArtifactInfo]
    total: int
