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
    # v34: peer that owns the on-disk file. Empty string = pre-federation
    # local. Callers consult this before reading ``path`` directly so they
    # know whether to GET /artifacts/{id}/content (which federates) instead
    # of opening the local file.
    origin_peer: str = ""


class ArtifactSearchResponse(BaseModel):
    artifacts: list[ArtifactInfo]
    total: int
