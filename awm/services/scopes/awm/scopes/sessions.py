"""Session log CRUD (v1 modular).

v1 schema change: ``session_logs`` no longer carries ``agent_id`` (an
agents-table FK); instead it carries ``project`` and ``scope`` inline.
All SQL goes through ScopesDAO.

Embeddings indexing is reimplemented against the scopes service's own
``embeddings`` table via ``awm.persistence.embeddings``.
"""

from __future__ import annotations

import json
import subprocess

from awm.config import SKILLS_DIR
from awm.scopes.dao import ScopesDAO
from awm.scopes.identity import ms_to_iso, now_ms
from awm.scopes.models import (
    SessionLogCreateRequest,
    SessionLogEntry,
    SessionLogListResponse,
    SessionLogContentResponse,
    SessionLogPreview,
    SessionSearchResponse,
)

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
    """Map a session_logs row (with inline project/scope) to SessionLogEntry."""
    return SessionLogEntry(
        id=row["id"],
        project=row["project"],
        scope=row["scope"],
        file_path=row["file_path"] or "",
        git_commit=row["git_commit"],
        logged_at=ms_to_iso(row["created_at"]) or "",
        title=row["title"],
        agent_id=f"{row['project']}/{row['scope']}",  # synthetic for API compat
        skill_path=row["skill_path"],
        outcome=row["outcome"],
        deviations=row["deviations"],
        suggestions=row["suggestions"],
        skill_version=row["skill_version"],
        resolved_at=ms_to_iso(row["resolved_at"]),
        resolution=row["resolution"],
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


def _index_session(session_id: int) -> None:
    """Upsert a session embedding in the scopes DB. Silently no-ops on failure."""
    try:
        from awm.persistence.embeddings import upsert_embedding
        from awm.persistence.databases import get_connection
        dao = ScopesDAO()
        row = dao.query_one(
            "SELECT id, project, scope, summary, title FROM session_logs WHERE id=?",
            (session_id,),
        )
        if row is None:
            return
        text = " ".join(filter(None, [
            row["title"], row["summary"],
            f"{row['project']}/{row['scope']}",
        ]))
        conn = get_connection("scopes")
        try:
            upsert_embedding(conn, "session", str(session_id), text[:500])
        finally:
            conn.close()
    except Exception:
        pass


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

    dao = ScopesDAO()
    with dao.transaction() as conn:
        dao2 = ScopesDAO(conn=conn)
        dao2.execute(
            "INSERT INTO session_logs "
            "(project, scope, created_at, file_path, git_commit, summary, "
            " metadata, content, skill_path, outcome, deviations, "
            " suggestions, skill_version, title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (req.project, req.scope, created_at_ms, "", None, req.summary,
             metadata, content, req.skill_path, req.outcome,
             req.deviations, req.suggestions, skill_version, req.title),
        )

    row = dao.query_one(
        "SELECT * FROM session_logs WHERE project=? AND scope=? "
        "ORDER BY created_at DESC LIMIT 1",
        (req.project, req.scope),
    )
    entry = _row_to_entry(row)

    _index_session(entry.id)

    return entry


def get_session(session_id: int | str) -> SessionLogContentResponse:
    """Look up a session by its integer id."""
    try:
        sid = int(str(session_id).split("@", 1)[0])
    except ValueError as exc:
        raise FileNotFoundError(f"Session log {session_id} not found") from exc
    dao = ScopesDAO()
    row = dao.query_one("SELECT * FROM session_logs WHERE id=?", (sid,))
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

    if entry.resolved_at:
        content += (
            f"\n## Resolution\n\n**Resolved:** {entry.resolved_at[:10]}\n\n"
            f"{entry.resolution}\n"
        )

    return SessionLogContentResponse(entry=entry, content=content)


def _display_title(row) -> str:
    if row["title"]:
        return row["title"]
    s = row["summary"]
    return s[:80] + "…" if len(s) > 80 else s


def _row_to_preview(row) -> SessionLogPreview:
    raw_summary = row.get("summary") or ""
    truncated = len(raw_summary) > _SUMMARY_PREVIEW_CHARS
    summary = raw_summary[:_SUMMARY_PREVIEW_CHARS]
    if truncated:
        summary = summary + "…"
    return SessionLogPreview(
        id=row["id"],
        project=row["project"],
        scope=row["scope"],
        logged_at=ms_to_iso(row["created_at"]) or "",
        summary=summary,
        title=row["title"],
        agent_id=f"{row['project']}/{row['scope']}",
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
    dao = ScopesDAO()
    existing = dao.query_one("SELECT id FROM session_logs WHERE id=?", (sid,))
    if existing is None:
        raise FileNotFoundError(f"Session log {session_id} not found")
    with dao.transaction() as conn:
        ScopesDAO(conn=conn).execute(
            "UPDATE session_logs SET resolved_at=?, resolution=? WHERE id=?",
            (now_ms(), resolution, sid),
        )
    row = dao.query_one("SELECT * FROM session_logs WHERE id=?", (sid,))
    return _row_to_entry(row)


def search_sessions(
    project: str | None = None,
    scope: str | None = None,
    skill_path: str | None = None,
    query: str | None = None,
    status: str = "open",
    limit: int = 50,
    offset: int = 0,
) -> SessionSearchResponse:
    """Search session logs, returning previews. Defaults to status='open'."""
    dao = ScopesDAO()
    sql = "SELECT * FROM session_logs WHERE 1=1"
    params: list = []
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
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = dao.query_all(sql, params)
    entries = [_row_to_preview(r) for r in rows]

    if not query:
        return SessionSearchResponse(entries=entries, total=len(entries))

    keyword_keys = {str(e.id) for e in entries}

    def _materialize(source_id: str):
        try:
            sid = int(source_id)
        except ValueError:
            return None
        d = ScopesDAO()
        r = d.query_one("SELECT * FROM session_logs WHERE id=?", (sid,))
        if r is None:
            return None
        if status == "open" and r["resolved_at"]:
            return None
        if status == "resolved" and not r["resolved_at"]:
            return None
        if project and r["project"] != project:
            return None
        if scope and r["scope"] != scope:
            return None
        if skill_path and r["skill_path"] != skill_path:
            return None
        return _row_to_preview(r)

    try:
        from awm.persistence.embeddings import hybrid_augment
        from awm.persistence.databases import get_connection
        conn = get_connection("scopes")
        try:
            merged = hybrid_augment(
                conn, query, source_type="session",
                keyword_hits=entries, keyword_keys=keyword_keys,
                materialize=_materialize,
            )
        finally:
            conn.close()
    except Exception:
        merged = entries
    return SessionSearchResponse(entries=merged, total=len(merged))
