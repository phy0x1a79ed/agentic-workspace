"""Experience CRUD — log and query execution traces attached to skills."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone

from awm.config import SKILLS_DIR
from awm.db import get_connection
from awm.models import (
    ExperienceLogRequest,
    ExperienceEntry,
    ExperienceListResponse,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_skills_git_hash() -> str | None:
    """Get the current git commit hash of the skills directory."""
    try:
        # Skills dir is inside the awm package which is in the workspace git repo
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", str(SKILLS_DIR)],
            cwd=str(SKILLS_DIR.parent.parent),  # workspace root
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()[:12]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _row_to_entry(row) -> ExperienceEntry:
    return ExperienceEntry(
        id=row["id"],
        skill_path=row["skill_path"],
        skill_version=row["skill_version"],
        project=row["project"],
        scope=row["scope"],
        agent_id=row["agent_id"],
        outcome=row["outcome"],
        summary=row["summary"],
        deviations=row["deviations"],
        suggestions=row["suggestions"],
        created_at=row["created_at"],
    )


def log_experience(req: ExperienceLogRequest) -> ExperienceEntry:
    """Record an experience. Auto-captures skill git hash if skill_path is provided."""
    skill_version = None
    if req.skill_path:
        skill_version = _get_skills_git_hash()

    now = _now_iso()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO experiences
                (skill_path, skill_version, project, scope, agent_id, outcome,
                 summary, deviations, suggestions, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (req.skill_path, skill_version, req.project, req.scope,
             req.agent_id, req.outcome, req.summary, req.deviations,
             req.suggestions, None, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM experiences WHERE id = last_insert_rowid()"
        ).fetchone()
        entry = _row_to_entry(row)
    finally:
        conn.close()

    # Auto-index for semantic search (best-effort, don't fail the log)
    try:
        from awm.services.embeddings import index_experience
        index_experience(entry.id)
    except Exception:
        pass

    return entry


def list_experiences(
    skill_path: str | None = None,
    project: str | None = None,
    scope: str | None = None,
    limit: int = 50,
) -> ExperienceListResponse:
    """Query experiences with optional filters."""
    conn = get_connection()
    try:
        query = "SELECT * FROM experiences WHERE 1=1"
        params: list = []
        if skill_path:
            query += " AND skill_path = ?"
            params.append(skill_path)
        if project:
            query += " AND project = ?"
            params.append(project)
        if scope:
            query += " AND scope = ?"
            params.append(scope)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        entries = [_row_to_entry(r) for r in rows]
        return ExperienceListResponse(experiences=entries, total=len(entries))
    finally:
        conn.close()
