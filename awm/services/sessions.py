"""Session log CRUD: DB metadata + file content."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from awm.config import MAIN_DIR
from awm.db import get_connection
from awm.models import (
    SessionLogCreateRequest,
    SessionLogEntry,
    SessionLogListResponse,
    SessionLogContentResponse,
)


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_entry(row) -> SessionLogEntry:
    return SessionLogEntry(
        id=row["id"],
        project=row["project"],
        task=row["task"],
        file_path=row["file_path"],
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
        f"",
        f"**Task:** {req.project}/{req.task}",
        f"",
        f"**Agent:** {req.agent_id}",
        f"",
        f"## Session Summary",
        f"",
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
    """Append a session log entry to experiences.md and record in DB."""
    workspace_dir = MAIN_DIR / req.project / req.task
    if not workspace_dir.exists():
        raise FileNotFoundError(
            f"Task workspace not found: main/{req.project}/{req.task}"
        )

    exp_file = workspace_dir / "experiences.md"
    rel_path = f"main/{req.project}/{req.task}/experiences.md"
    logged_at = _now_iso()

    # Format and append the entry
    entry_text = _format_entry(req, logged_at)
    with open(exp_file, "a", encoding="utf-8") as f:
        f.write(entry_text)

    # No git commit — experiences.md lives in workspace, not repo

    # Store metadata in DB
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
                (project, task, file_path, git_commit, logged_at, summary, agent_id, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (req.project, req.task, rel_path, None, logged_at,
             req.summary, req.agent_id, metadata),
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
    task: str | None = None,
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
        if task:
            query += " AND task = ?"
            params.append(task)
        query += " ORDER BY logged_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        entries = [_row_to_entry(r) for r in rows]
        return SessionLogListResponse(entries=entries, total=len(entries))
    finally:
        conn.close()


def get_session(session_id: int) -> SessionLogContentResponse:
    """Look up a session by ID and return metadata + content."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM session_logs WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise FileNotFoundError(f"Session log {session_id} not found")
        entry = _row_to_entry(row)
    finally:
        conn.close()

    content = ""

    # Legacy fallback: try reading from git if we have a commit hash
    if entry.git_commit:
        # Legacy entries stored in tasks/ worktrees
        from awm.config import TASKS_DIR, REPOS_DIR
        for base_dir in [REPOS_DIR, TASKS_DIR]:
            task_dir = base_dir / entry.project / entry.task
            if task_dir.exists():
                r = _run([
                    "git", "-C", str(task_dir),
                    "show", f"{entry.git_commit}:{Path('experiences.md')}",
                ])
                if r.returncode == 0:
                    content = r.stdout
                    break

    # Primary: read from workspace
    if not content:
        exp_file = MAIN_DIR / entry.project / entry.task / "experiences.md"
        if exp_file.exists():
            content = exp_file.read_text(encoding="utf-8")

    # Fallback: try legacy path
    if not content:
        from awm.config import TASKS_DIR
        legacy_file = TASKS_DIR / entry.project / entry.task / "experiences.md"
        if legacy_file.exists():
            content = legacy_file.read_text(encoding="utf-8")

    return SessionLogContentResponse(entry=entry, content=content)


def reflect(
    project: str | None = None,
    task: str | None = None,
    query: str | None = None,
) -> SessionLogListResponse:
    """Search across session summaries for reflection/learning."""
    conn = get_connection()
    try:
        sql = "SELECT * FROM session_logs WHERE 1=1"
        params: list = []
        if project:
            sql += " AND project = ?"
            params.append(project)
        if task:
            sql += " AND task = ?"
            params.append(task)
        if query:
            sql += " AND (summary LIKE ? OR metadata LIKE ?)"
            like = f"%{query}%"
            params.extend([like, like])
        sql += " ORDER BY logged_at DESC"

        rows = conn.execute(sql, params).fetchall()
        entries = [_row_to_entry(r) for r in rows]
        return SessionLogListResponse(entries=entries, total=len(entries))
    finally:
        conn.close()
