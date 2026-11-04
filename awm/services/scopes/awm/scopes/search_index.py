"""The scopes service's search index: scopes and projects in the retrieval engine.

Writes go through the service's one index worker (:class:`ServiceIndex`). A
supervised backfill walks the tables at startup and every few hours. Posts and briefs live in the
journal service; a scope's brief is read from it over the gateway.
"""

from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path
from typing import Any, Iterable

from awm.persistence import embeddings as engine
from awm.persistence.service_index import ServiceIndex
from awm.persistence.timestamps import ms_to_date

_README_CHARS = 2000
_README_CHECKOUTS = ("release", "main", "dev")


def _read(path: Path, limit: int | None = None) -> str:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    return text[:limit] if limit else text


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


def _scope_brief(project: str, scope: str) -> str:
    """The scope's brief from the journal service; empty when it has none or cannot answer."""
    try:
        from awm.gatewayclient import call_sync
        res = call_sync("journal", "brief_read", {"project": project, "scope": scope}, timeout=5.0)
        return ((res or {}).get("brief") or {}).get("text") or ""
    except Exception:  # noqa: BLE001
        return ""


def scope_document(project: str, scope: str, created_at: int | None) -> engine.Document:
    """The scope's journal brief."""
    body = _scope_brief(project, scope)
    return engine.Document(f"{project}/{scope}", body, title=f"{project}/{scope}",
                           date=ms_to_date(created_at))


def project_document(name: str, scopes: Iterable[str]) -> engine.Document:
    """The project's scope names and the head of its README."""
    from awm.config import PROJECTS_DIR

    readme = ""
    for checkout in _README_CHECKOUTS:
        readme = _read(PROJECTS_DIR / name / checkout / "README.md", _README_CHARS)
        if readme:
            break
    names = sorted(set(scopes))
    body = "\n\n".join(x for x in (f"Scopes: {', '.join(names)}" if names else "", readme) if x)
    return engine.Document(name, body, title=name)


def _scope_rows(conn, project: str | None = None, scope: str | None = None):
    sql = ("SELECT p.name AS project, a.scope, a.created_at"
           " FROM agents a JOIN projects p ON p.id = a.project_id WHERE 1=1")
    params: list = []
    if project is not None:
        sql += " AND p.name = ? AND a.scope = ?"
        params += [project, scope]
    latest: dict[tuple[str, str], Any] = {}
    for r in conn.execute(sql + " ORDER BY a.created_at", params):
        latest[(r["project"], r["scope"])] = r
    return latest.values()


def _scope_docs(conn) -> list[engine.Document]:
    return [scope_document(r["project"], r["scope"], r["created_at"])
            for r in _scope_rows(conn)]


def _project_docs(conn) -> list[engine.Document]:
    from awm.config import PROJECTS_DIR

    scopes: dict[str, list[str]] = {}
    for r in conn.execute("SELECT p.name, a.scope FROM projects p"
                          " LEFT JOIN agents a ON a.project_id = p.id"):
        scopes.setdefault(r[0], []).extend([r[1]] if r[1] else [])
    if PROJECTS_DIR.is_dir():
        for child in PROJECTS_DIR.iterdir():
            if (child / ".bare").is_dir():
                scopes.setdefault(child.name, [])
    return [project_document(n, s) for n, s in sorted(scopes.items())]


# ---------------------------------------------------------------------------
# Writes (background)
# ---------------------------------------------------------------------------


def _index_scope(conn, project: str, scope: str) -> None:
    rows = list(_scope_rows(conn, project, scope))
    if not rows:
        engine.delete_document(conn, "scope", f"{project}/{scope}")
        return
    r = rows[-1]
    engine.index_document(conn, "scope", scope_document(project, scope, r["created_at"]))


def _index_project(conn, name: str) -> None:
    scopes = [r[0] for r in conn.execute(
        "SELECT a.scope FROM agents a JOIN projects p ON p.id = a.project_id WHERE p.name=?",
        (name,))]
    engine.index_document(conn, "project", project_document(name, scopes))


def index_scope(project: str, scope: str) -> Future:
    return INDEX.submit(_index_scope, project, scope)


def index_project(name: str) -> Future:
    return INDEX.submit(_index_project, name)


INDEX = ServiceIndex("scopes", lambda conn: {"scope": _scope_docs(conn),
                                             "project": _project_docs(conn)})
flush, ensure_schema, run_reindex = INDEX.flush, INDEX.ensure_schema, INDEX.run_reindex
backfill_loop, search = INDEX.backfill_loop, INDEX.search
