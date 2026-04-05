"""Session log CRUD: DB-only storage."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from awm.config import MAIN_DIR
from awm.db import get_connection
from awm.models import (
    SessionLogCreateRequest,
    SessionLogEntry,
    SessionLogListResponse,
    SessionLogContentResponse,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_entry(row) -> SessionLogEntry:
    return SessionLogEntry(
        id=row["id"],
        project=row["project"],
        scope=row["scope"],
        file_path=row["file_path"] or "",
        git_commit=row["git_commit"],
        logged_at=row["logged_at"],
        summary=row["summary"],
        agent_id=row["agent_id"],
    )


def _format_entry(req: SessionLogCreateRequest, logged_at: str) -> str:
    """Format a session log entry as markdown following the experience-entry template."""
    date_str = logged_at[:10]
    lines = [
        f"\n**Date:** {date_str}",
        "",
        f"**Scope:** {req.project}/{req.scope}",
        "",
        f"**Agent:** {req.agent_id}",
        "",
        "## Session Summary",
        "",
        req.summary,
    ]

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

    lines.extend(["", "---", ""])
    return "\n".join(lines)


def log_session(req: SessionLogCreateRequest) -> SessionLogEntry:
    """Record a session log entry in the database."""
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
        conn.execute(
            """
            INSERT INTO session_logs
                (project, scope, file_path, git_commit, logged_at, summary, agent_id, metadata, content)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (req.project, req.scope, "", None, logged_at,
             req.summary, req.agent_id, metadata, content),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM session_logs WHERE id = last_insert_rowid()"
        ).fetchone()
        return _row_to_entry(row)
    finally:
        conn.close()


def list_sessions(
    project: str | None = None,
    scope: str | None = None,
    limit: int = 50,
) -> SessionLogListResponse:
    """Query session logs with optional filters."""
    conn = get_connection()
    try:
        query = "SELECT * FROM session_logs WHERE 1=1"
        params: list = []
        if project:
            query += " AND project = ?"
            params.append(project)
        if scope:
            query += " AND scope = ?"
            params.append(scope)
        query += " ORDER BY logged_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        entries = [_row_to_entry(r) for r in rows]
        return SessionLogListResponse(entries=entries, total=len(entries))
    finally:
        conn.close()


def get_session(session_id: int) -> SessionLogContentResponse:
    """Look up a session by ID and return metadata + content from DB."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM session_logs WHERE id = ?", (session_id,)
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
            if meta.get("decisions"):
                parts.append("\nDecisions: " + ", ".join(meta["decisions"]))
            if meta.get("issues"):
                parts.append("\nIssues: " + ", ".join(meta["issues"]))
            if meta.get("next_steps"):
                parts.append("\nNext steps: " + ", ".join(meta["next_steps"]))
            content = "\n".join(parts)
    finally:
        conn.close()

    return SessionLogContentResponse(entry=entry, content=content)


# ---------------------------------------------------------------------------
# Experiences.md migration
# ---------------------------------------------------------------------------


def migrate_experiences() -> dict:
    """Migrate experiences.md files into the database.

    Returns stats: {imported, skipped, files_processed}
    """
    imported = 0
    skipped = 0
    files_processed = 0

    conn = get_connection()
    try:
        # Search both old (*/tasks/*/experiences.md) and new layouts
        exp_files = list(MAIN_DIR.glob("*/tasks/*/experiences.md"))
        exp_files.extend(MAIN_DIR.glob("*/scopes/*/experiences.md"))

        for exp_file in exp_files:
            scope_name = exp_file.parent.name
            project_name = exp_file.parent.parent.parent.name

            text = exp_file.read_text(encoding="utf-8").strip()
            if not text:
                continue

            files_processed += 1

            # Strip header
            text = re.sub(
                r"^#\s+Experiences\s+--\s+\S+\s*\n*(?:##\s+Log\s*\n*)?", "", text
            )
            # Strip completion footer
            text = re.sub(r"\n*##\s+Completed:.*$", "", text, flags=re.DOTALL)
            text = text.strip()

            if not text:
                continue

            # Split entries on --- separator
            chunks = re.split(r"\n---\n", text)

            for chunk in chunks:
                chunk = chunk.strip()
                if not chunk:
                    continue

                entry_data = _parse_structured_entry(chunk, project_name, scope_name)

                if entry_data is None:
                    # Free-form entry
                    first_line = chunk.split("\n")[0].strip()
                    if first_line.startswith("#"):
                        first_line = first_line.lstrip("#").strip()
                    entry_data = {
                        "project": project_name,
                        "scope": scope_name,
                        "logged_at": _now_iso(),
                        "summary": first_line[:200],
                        "agent_id": "unknown",
                        "content": chunk,
                        "metadata": None,
                    }

                # Dedup check
                existing = conn.execute(
                    "SELECT id FROM session_logs WHERE project=? AND scope=? AND logged_at=? AND agent_id=?",
                    (
                        entry_data["project"],
                        entry_data["scope"],
                        entry_data["logged_at"],
                        entry_data["agent_id"],
                    ),
                ).fetchone()

                if existing:
                    skipped += 1
                    continue

                conn.execute(
                    """INSERT INTO session_logs
                        (project, scope, file_path, git_commit, logged_at, summary, agent_id, metadata, content)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entry_data["project"],
                        entry_data["scope"],
                        "",
                        None,
                        entry_data["logged_at"],
                        entry_data["summary"],
                        entry_data["agent_id"],
                        entry_data["metadata"],
                        entry_data["content"],
                    ),
                )
                imported += 1

        # Backfill content for existing DB rows that have empty content
        rows = conn.execute(
            "SELECT id, project, scope, summary, metadata FROM session_logs WHERE content IS NULL OR content = ''"
        ).fetchall()
        for row in rows:
            meta: dict = {}
            if row["metadata"]:
                try:
                    meta = json.loads(row["metadata"])
                except json.JSONDecodeError:
                    pass

            parts = [row["summary"]]
            if meta.get("decisions"):
                parts.append(
                    "\nDecisions:\n"
                    + "\n".join(f"- {d}" for d in meta["decisions"])
                )
            if meta.get("issues"):
                parts.append(
                    "\nIssues:\n" + "\n".join(f"- {i}" for i in meta["issues"])
                )
            if meta.get("next_steps"):
                parts.append(
                    "\nNext steps:\n"
                    + "\n".join(f"- [ ] {s}" for s in meta["next_steps"])
                )

            backfill_content = "\n".join(parts)
            conn.execute(
                "UPDATE session_logs SET content=? WHERE id=?",
                (backfill_content, row["id"]),
            )

        conn.commit()
    finally:
        conn.close()

    return {
        "imported": imported,
        "skipped": skipped,
        "files_processed": files_processed,
    }


def _parse_structured_entry(
    chunk: str, project: str, scope: str
) -> dict | None:
    """Parse a structured entry (from _format_entry). Returns None if not structured."""
    date_match = re.search(r"\*\*Date:\*\*\s*(\d{4}-\d{2}-\d{2})", chunk)
    if not date_match:
        return None

    date_str = date_match.group(1)
    logged_at = f"{date_str}T00:00:00+00:00"

    agent_match = re.search(r"\*\*Agent:\*\*\s*(\S+)", chunk)
    agent_id = agent_match.group(1) if agent_match else "unknown"

    summary_match = re.search(
        r"##\s+Session Summary\s*\n+(.*?)(?=\n##|\Z)", chunk, re.DOTALL
    )
    summary = summary_match.group(1).strip() if summary_match else chunk[:200]

    decisions: list[str] = []
    decisions_match = re.search(
        r"##\s+Decisions Made\s*\n+(.*?)(?=\n##|\Z)", chunk, re.DOTALL
    )
    if decisions_match:
        for line in decisions_match.group(1).strip().split("\n"):
            line = line.strip()
            if line.startswith("- "):
                decisions.append(line[2:])

    issues: list[str] = []
    issues_match = re.search(
        r"##\s+Gotchas\s*/\s*Issues\s*\n+(.*?)(?=\n##|\Z)", chunk, re.DOTALL
    )
    if issues_match:
        for line in issues_match.group(1).strip().split("\n"):
            line = line.strip()
            if line.startswith("- "):
                issues.append(line[2:])

    next_steps: list[str] = []
    next_match = re.search(
        r"##\s+Next Steps\s*\n+(.*?)(?=\n##|\Z)", chunk, re.DOTALL
    )
    if next_match:
        for line in next_match.group(1).strip().split("\n"):
            line = line.strip()
            if line.startswith("- [ ] "):
                next_steps.append(line[6:])
            elif line.startswith("- "):
                next_steps.append(line[2:])

    metadata = None
    if decisions or issues or next_steps:
        metadata = json.dumps(
            {"decisions": decisions, "issues": issues, "next_steps": next_steps}
        )

    return {
        "project": project,
        "scope": scope,
        "logged_at": logged_at,
        "summary": summary,
        "agent_id": agent_id,
        "content": chunk,
        "metadata": metadata,
    }
