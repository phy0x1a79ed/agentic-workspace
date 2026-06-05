"""Session log CRUD: DB-only storage (v37)."""

from __future__ import annotations

import json
import subprocess

from awm.config import SKILLS_DIR
from awm.db import get_connection
from awm.models import (
    SessionLogCreateRequest,
    SessionLogEntry,
    SessionLogListResponse,
    SessionLogContentResponse,
    SessionLogPreview,
    SessionSearchResponse,
)
from awm.services import identity
from awm.services.identity import iso_to_ms, ms_to_iso, now_ms

_SUMMARY_PREVIEW_CHARS = 240


def _get_skills_git_hash() -> str | None:
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", str(SKILLS_DIR)],
            cwd=str(SKILLS_DIR.parent.parent),
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()[:12]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _row_to_entry(row) -> SessionLogEntry:
    return SessionLogEntry(
        id=row["id"],
        project=row["project_name"],
        scope=row["scope_name"],
        file_path=row["file_path"] or "",
        git_commit=row["git_commit"],
        logged_at=ms_to_iso(row["created_at"]) or "",
        title=row["title"],
        agent_id=row["agent_id"],
        skill_path=row["skill_path"],
        outcome=row["outcome"],
        deviations=row["deviations"],
        suggestions=row["suggestions"],
        skill_version=row["skill_version"],
        resolved_at=ms_to_iso(row["resolved_at"]),
        resolution=row["resolution"],
    )


_BASE_SELECT = (
    "SELECT s.*, p.name AS project_name, a.scope AS scope_name "
    "FROM session_logs s "
    "JOIN agents a ON a.id = s.agent_id "
    "JOIN projects p ON p.id = a.project_id"
)


def _format_entry(req: SessionLogCreateRequest, logged_at_iso: str) -> str:
    date_str = logged_at_iso[:10]
    lines = []
    if req.title:
        lines.extend([f"# {req.title}", ""])
    lines.extend([
        f"**Date:** {date_str}",
        "",
        f"**Scope:** {req.project}/{req.scope}",
        "",
        f"**Agent:** {req.agent_id}",
    ])
    if req.skill_path:
        lines.extend(["", f"**Skill:** {req.skill_path}"])
    if req.outcome:
        lines.extend(["", f"**Outcome:** {req.outcome}"])
    lines.extend([
        "",
        "## Session Summary",
        "",
        req.summary,
    ])

    if req.decisions:
        lines.extend(["", "## Decisions Made", ""])
        for d in req.decisions:
            lines.append(f"- {d}")

    if req.issues:
        lines.extend(["", "## Gotchas / Issues", ""])
        for i in req.issues:
            lines.append(f"- {i}")

    if req.next_steps:
        lines.extend(["", "## Next Steps", ""])
        for ns in req.next_steps:
            lines.append(f"- [ ] {ns}")

    if req.deviations:
        lines.extend(["", "## Deviations", "", req.deviations])

    if req.suggestions:
        lines.extend(["", "## Suggestions", "", req.suggestions])

    lines.extend(["", "---", ""])
    return "\n".join(lines)


def _ensure_agent_for_log(project: str, scope: str, *, conn) -> str:
    """Resolve (project, scope) to an agent id, creating allocated rows
    (and the project) on demand so a fresh session_log doesn't blow up
    on an unseeded scope."""
    pid = identity.project_id_for_name(project, conn=conn)
    if pid is None:
        from awm.config import PROJECTS_DIR
        bare = PROJECTS_DIR / project / ".bare"
        identity.ensure_project(project, repo_path=str(bare), conn=conn)
    return identity.ensure_agent(project, scope, conn=conn)


def log_session(req: SessionLogCreateRequest) -> SessionLogEntry:
    """Record a session log entry."""
    skill_version = None
    if req.skill_path:
        skill_version = _get_skills_git_hash()

    created_at_ms = now_ms()
    logged_at_iso = ms_to_iso(created_at_ms) or ""
    content = _format_entry(req, logged_at_iso)

    metadata = None
    if req.decisions or req.issues or req.next_steps:
        metadata = json.dumps({
            "decisions": req.decisions,
            "issues": req.issues,
            "next_steps": req.next_steps,
        })

    conn = get_connection()
    try:
        agent_id = _ensure_agent_for_log(req.project, req.scope, conn=conn)
        cur = conn.execute(
            "INSERT INTO session_logs "
            "(agent_id, created_at, file_path, git_commit, summary, "
            " metadata, content, skill_path, outcome, deviations, "
            " suggestions, skill_version, title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (agent_id, created_at_ms, "", None, req.summary,
             metadata, content, req.skill_path, req.outcome,
             req.deviations, req.suggestions, skill_version, req.title),
        )
        sid = cur.lastrowid
        conn.commit()
        row = conn.execute(
            f"{_BASE_SELECT} WHERE s.id=?", (sid,),
        ).fetchone()
        entry = _row_to_entry(row)
    finally:
        conn.close()

    try:
        from awm.services.embeddings import index_session
        index_session(entry.id)
    except Exception:
        pass

    return entry


def list_sessions(
    project: str | None = None,
    scope: str | None = None,
    skill_path: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> SessionLogListResponse:
    conn = get_connection()
    try:
        query = f"{_BASE_SELECT} WHERE 1=1"
        params: list = []
        if project:
            query += " AND p.name = ?"
            params.append(project)
        if scope:
            query += " AND a.scope = ?"
            params.append(scope)
        if skill_path:
            query += " AND s.skill_path = ?"
            params.append(skill_path)
        if status == "open":
            query += " AND s.resolved_at IS NULL"
        elif status == "resolved":
            query += " AND s.resolved_at IS NOT NULL"
        query += " ORDER BY s.created_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        entries = [_row_to_entry(r) for r in rows]
        return SessionLogListResponse(entries=entries, total=len(entries))
    finally:
        conn.close()


def get_session(session_id: int | str) -> SessionLogContentResponse:
    """Look up a session by its INTEGER id. Cross-peer ``42@peer`` ids
    are no longer routable post-v37 (session_logs aren't replicated)."""
    try:
        sid = int(str(session_id).split("@", 1)[0])
    except ValueError as exc:
        raise FileNotFoundError(f"Session log {session_id} not found") from exc
    conn = get_connection()
    try:
        row = conn.execute(
            f"{_BASE_SELECT} WHERE s.id=?", (sid,),
        ).fetchone()
        if row is None:
            raise FileNotFoundError(f"Session log {session_id} not found")
        entry = _row_to_entry(row)
        content = row["content"] or ""
        if not content:
            meta: dict = {}
            if row["metadata"]:
                try:
                    meta = json.loads(row["metadata"])
                except json.JSONDecodeError:
                    pass
            parts = [row["summary"]]
            if row["outcome"]:
                parts.append(f"\nOutcome: {row['outcome']}")
            if meta.get("decisions"):
                parts.append("\nDecisions: " + ", ".join(meta["decisions"]))
            if meta.get("issues"):
                parts.append("\nIssues: " + ", ".join(meta["issues"]))
            if meta.get("next_steps"):
                parts.append("\nNext steps: " + ", ".join(meta["next_steps"]))
            if row["deviations"]:
                parts.append(f"\nDeviations: {row['deviations']}")
            if row["suggestions"]:
                parts.append(f"\nSuggestions: {row['suggestions']}")
            content = "\n".join(parts)
    finally:
        conn.close()

    if entry.resolved_at:
        content += f"\n## Resolution\n\n**Resolved:** {entry.resolved_at[:10]}\n\n{entry.resolution}\n"

    return SessionLogContentResponse(entry=entry, content=content)


def _display_title(row) -> str:
    if row["title"]:
        return row["title"]
    s = row["summary"]
    return s[:80] + "…" if len(s) > 80 else s


def _row_to_preview(row) -> SessionLogPreview:
    raw_len = row["summary_len"] or 0
    truncated = raw_len > _SUMMARY_PREVIEW_CHARS
    summary = row["summary_preview"] or ""
    if truncated:
        summary = summary + "…"
    return SessionLogPreview(
        id=row["id"],
        project=row["project_name"],
        scope=row["scope_name"],
        logged_at=ms_to_iso(row["created_at"]) or "",
        summary=summary,
        title=row["title"],
        agent_id=row["agent_id"],
        skill_path=row["skill_path"],
        outcome=row["outcome"],
        resolved_at=ms_to_iso(row["resolved_at"]),
        summary_truncated=truncated,
    )


def resolve_session(session_id: int | str, resolution: str) -> SessionLogEntry:
    try:
        sid = int(str(session_id).split("@", 1)[0])
    except ValueError as exc:
        raise FileNotFoundError(f"Session log {session_id} not found") from exc
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT id FROM session_logs WHERE id=?", (sid,),
        ).fetchone()
        if existing is None:
            raise FileNotFoundError(f"Session log {session_id} not found")
        conn.execute(
            "UPDATE session_logs SET resolved_at=?, resolution=? WHERE id=?",
            (now_ms(), resolution, sid),
        )
        conn.commit()
        row = conn.execute(f"{_BASE_SELECT} WHERE s.id=?", (sid,)).fetchone()
        return _row_to_entry(row)
    finally:
        conn.close()


def search_sessions(
    project: str | None = None,
    scope: str | None = None,
    skill_path: str | None = None,
    query: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> SessionSearchResponse:
    conn = get_connection()
    try:
        sql = (
            "SELECT s.id, s.created_at, s.title, s.skill_path, s.outcome, "
            "       s.resolved_at, s.agent_id, "
            "       SUBSTR(s.summary, 1, ?) AS summary_preview, "
            "       LENGTH(s.summary) AS summary_len, "
            "       p.name AS project_name, a.scope AS scope_name "
            "FROM session_logs s "
            "JOIN agents a ON a.id = s.agent_id "
            "JOIN projects p ON p.id = a.project_id "
            "WHERE 1=1"
        )
        params: list = [_SUMMARY_PREVIEW_CHARS]
        if project:
            sql += " AND p.name = ?"
            params.append(project)
        if scope:
            sql += " AND a.scope = ?"
            params.append(scope)
        if skill_path:
            sql += " AND s.skill_path = ?"
            params.append(skill_path)
        if query:
            sql += " AND (s.title LIKE ? OR s.summary LIKE ? OR s.content LIKE ?)"
            like = f"%{query}%"
            params.extend([like, like, like])
        if status == "open":
            sql += " AND s.resolved_at IS NULL"
        elif status == "resolved":
            sql += " AND s.resolved_at IS NOT NULL"
        sql += " ORDER BY s.created_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        entries = [_row_to_preview(r) for r in rows]
        return SessionSearchResponse(entries=entries, total=len(entries))
    finally:
        conn.close()
