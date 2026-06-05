"""Session log CRUD: DB-only storage."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

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
from awm.services.network import peers as peer_svc
from awm.services.replication.schema import new_uuid, next_legacy_id

_SUMMARY_PREVIEW_CHARS = 240


def _local_peer_id() -> str:
    """Return the local peer id, or '' if PEER_FILE hasn't been initialized.
    Used to stamp origin_peer on new session_log rows."""
    try:
        ident = peer_svc.get_local_identity()
    except Exception:
        return ""
    if ident is None:
        return ""
    return ident.get("peer_id") or ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_skills_git_hash() -> str | None:
    """Get the current git commit hash of the skills directory."""
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
        id=row["legacy_id"],
        project=row["project"],
        scope=row["scope"],
        file_path=row["file_path"] or "",
        git_commit=row["git_commit"],
        logged_at=row["logged_at"],
        title=row["title"],
        agent_id=row["agent_id"],
        skill_path=row["skill_path"],
        outcome=row["outcome"],
        deviations=row["deviations"],
        suggestions=row["suggestions"],
        skill_version=row["skill_version"],
        resolved_at=row["resolved_at"],
        resolution=row["resolution"],
    )


def _format_entry(req: SessionLogCreateRequest, logged_at: str) -> str:
    """Format a session log entry as markdown."""
    date_str = logged_at[:10]
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


def log_session(req: SessionLogCreateRequest) -> SessionLogEntry:
    """Record a session log entry in the database."""
    skill_version = None
    if req.skill_path:
        skill_version = _get_skills_git_hash()

    logged_at = _now_iso()
    content = _format_entry(req, logged_at)

    metadata = None
    if req.decisions or req.issues or req.next_steps:
        metadata = json.dumps({
            "decisions": req.decisions,
            "issues": req.issues,
            "next_steps": req.next_steps,
        })

    conn = get_connection()
    try:
        origin = _local_peer_id()
        legacy = next_legacy_id(conn, "session_logs", origin)
        uid = new_uuid()
        conn.execute(
            """
            INSERT INTO session_logs
                (uuid, legacy_id, origin_peer,
                 project, scope, file_path, git_commit, logged_at, summary,
                 title, agent_id, metadata, content, skill_path,
                 outcome, deviations, suggestions, skill_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (uid, legacy, origin,
             req.project, req.scope, "", None, logged_at,
             req.summary, req.title, req.agent_id, metadata, content,
             req.skill_path, req.outcome, req.deviations, req.suggestions,
             skill_version),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM session_logs WHERE uuid = ?", (uid,),
        ).fetchone()
        entry = _row_to_entry(row)
    finally:
        conn.close()

    # Auto-index for semantic search (best-effort)
    try:
        from awm.services.embeddings import index_session
        index_session(entry.id)
    except Exception:
        pass

    return entry


def get_session(session_id: int | str) -> SessionLogContentResponse:
    """Look up a session by its public ``legacy_id``. Accepts ``42`` or
    ``'42@<peer>'`` to disambiguate same-legacy_id rows from different
    peers post-replication."""
    legacy_id, peer = peer_svc.parse_id_ref(session_id)
    origin = peer if peer is not None else _local_peer_id()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM session_logs WHERE legacy_id = ? AND origin_peer = ?",
            (legacy_id, origin),
        ).fetchone()
        if row is None:
            raise FileNotFoundError(f"Session log {session_id} not found")
        entry = _row_to_entry(row)

        content = row["content"] or ""

        # Fallback for legacy rows without content
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
    """Return the title if set, otherwise truncate summary to 80 chars."""
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
        id=row["legacy_id"],
        project=row["project"],
        scope=row["scope"],
        logged_at=row["logged_at"],
        summary=summary,
        title=row["title"],
        agent_id=row["agent_id"],
        skill_path=row["skill_path"],
        outcome=row["outcome"],
        resolved_at=row["resolved_at"],
        summary_truncated=truncated,
    )


def resolve_session(session_id: int | str, resolution: str) -> SessionLogEntry:
    """Mark a session log entry as resolved. Accepts ``42`` or ``'42@<peer>'``."""
    legacy_id, peer = peer_svc.parse_id_ref(session_id)
    origin = peer if peer is not None else _local_peer_id()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT uuid FROM session_logs WHERE legacy_id = ? AND origin_peer = ?",
            (legacy_id, origin),
        ).fetchone()
        if row is None:
            raise FileNotFoundError(f"Session log {session_id} not found")
        uid = row["uuid"]
        conn.execute(
            "UPDATE session_logs SET resolved_at = ?, resolution = ? WHERE uuid = ?",
            (_now_iso(), resolution, uid),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM session_logs WHERE uuid = ?", (uid,),
        ).fetchone()
        return _row_to_entry(row)
    finally:
        conn.close()


def search_sessions(
    project: str | None = None,
    scope: str | None = None,
    skill_path: str | None = None,
    query: str | None = None,
    status: str = "open",
    limit: int = 50,
    offset: int = 0,
) -> SessionSearchResponse:
    """Search session logs, returning previews (no content). Defaults to
    status='open' (unresolved); pass status='all' for the full history."""
    sql = (
        "SELECT legacy_id, project, scope, logged_at, title, agent_id, "
        "skill_path, outcome, resolved_at, "
        "SUBSTR(summary, 1, ?) AS summary_preview, "
        "LENGTH(summary) AS summary_len "
        "FROM session_logs WHERE 1=1"
    )
    params: list = [_SUMMARY_PREVIEW_CHARS]
    if project:
        sql += " AND project = ?"
        params.append(project)
    if scope:
        sql += " AND scope = ?"
        params.append(scope)
    if skill_path:
        sql += " AND skill_path = ?"
        params.append(skill_path)
    if query:
        sql += " AND (title LIKE ? OR summary LIKE ? OR content LIKE ?)"
        like = f"%{query}%"
        params.extend([like, like, like])
    if status == "open":
        sql += " AND resolved_at IS NULL"
    elif status == "resolved":
        sql += " AND resolved_at IS NOT NULL"
    # status == "all" → no filter
    sql += " ORDER BY logged_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    conn = get_connection()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    entries = [_row_to_preview(r) for r in rows]

    if not query:
        return SessionSearchResponse(entries=entries, total=len(entries))

    keyword_keys = {str(e.id) for e in entries}

    def _materialize(source_id: str):
        try:
            sid = int(source_id)
        except ValueError:
            return None
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT legacy_id, project, scope, logged_at, title, agent_id, "
                "skill_path, outcome, resolved_at, "
                "SUBSTR(summary, 1, ?) AS summary_preview, "
                "LENGTH(summary) AS summary_len "
                "FROM session_logs WHERE legacy_id = ?",
                (_SUMMARY_PREVIEW_CHARS, sid),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        if status == "open" and row["resolved_at"]:
            return None
        if status == "resolved" and not row["resolved_at"]:
            return None
        if project and row["project"] != project:
            return None
        if scope and row["scope"] != scope:
            return None
        if skill_path and row["skill_path"] != skill_path:
            return None
        return _row_to_preview(row)

    try:
        from awm.services.embeddings import hybrid_augment
        merged = hybrid_augment(
            query, source_type="session",
            keyword_hits=entries, keyword_keys=keyword_keys,
            materialize=_materialize,
        )
    except Exception:
        merged = entries
    return SessionSearchResponse(entries=merged, total=len(merged))


