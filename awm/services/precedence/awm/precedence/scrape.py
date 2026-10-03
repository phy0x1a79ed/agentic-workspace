"""Scrapers that mine existing workspace history into precedence *candidates*.

Each function here is **pure**: it reads a source (memory files on disk, the
scopes service DB) and returns a list of candidate decision dicts in the
staging-manifest entry shape::

    {context, question, decision, created, source, source_ref, tags, notes}

They NEVER write the precedence DB. The seeding driver (``seed.py scrape``)
concatenates the candidates into one staging-manifest JSON, which a human
curates (drop noise, dedupe, fix phrasing, attach ``context-change`` notes)
before loading with ``precedence_import`` + ``precedence_embed``. This is T2 of
the precedence plan; the three sources, in descending signal:

1. **feedback memories** — ``feedback_*.md`` under the auto-memory dir. The
   user's own corrections, distilled. ``**Why:**`` → context, the frontmatter
   ``description`` → question, the body rule + ``**How to apply:**`` → decision.
2. **operator posts** — ``scope_posts`` authored by ``user:operator``. The
   cleanest bulk user-origin steering; heavily test-noise in practice, so a
   light gate drops throwaway/demo posts before curation sees them.
3. **journal decisions** — ``scope_posts`` of ``kind='journal'``,
   ``meta.decisions[]``. Agent-origin, so lower confidence and clearly tagged;
   the ``user_marked_only`` gate keeps just the items that record an explicit
   user decision, which is where the real preference signal lives.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def _iso_date(dt: datetime) -> str:
    """UTC calendar date (YYYY-MM-DD) — the granularity `created` needs."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _ms_to_iso(ts_ms: int | float | None) -> str | None:
    """scope_posts.ts is epoch **milliseconds** — convert to a UTC date."""
    if ts_ms is None:
        return None
    try:
        return _iso_date(datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=timezone.utc))
    except (ValueError, OSError, OverflowError):
        return None


def _mtime_iso(path: Path) -> str | None:
    try:
        return _iso_date(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Source 1 — feedback memories
# ---------------------------------------------------------------------------

_FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_KV_RE = re.compile(r'^\s*(name|description)\s*:\s*(.+?)\s*$', re.MULTILINE)
_WHY_RE = re.compile(r"\*\*Why:\*\*\s*(.*?)(?=\n\s*\*\*How to apply:\*\*|\Z)", re.DOTALL)
_HOW_RE = re.compile(
    r"\*\*How to apply:\*\*\s*(.*?)(?=\n\s*(?:\*\*|Related:)|\Z)", re.DOTALL
)


def _strip(text: str) -> str:
    return re.sub(r"[ \t]+", " ", (text or "")).strip().strip('"').strip()


def _parse_frontmatter(raw: str) -> tuple[dict[str, str], str]:
    m = _FRONT_RE.match(raw)
    if not m:
        return {}, raw
    fm = {k: _strip(v) for k, v in _KV_RE.findall(m.group(1))}
    return fm, raw[m.end():]


def scrape_memories(memory_dir: str | Path) -> list[dict[str, Any]]:
    """Candidates from ``feedback_*.md`` — the highest-signal, user-origin source."""
    memory_dir = Path(memory_dir)
    out: list[dict[str, Any]] = []
    for path in sorted(memory_dir.glob("feedback_*.md")):
        raw = path.read_text(encoding="utf-8")
        fm, body = _parse_frontmatter(raw)
        why = _WHY_RE.search(body)
        how = _HOW_RE.search(body)
        # Body intro = the rule as stated, before the Why/How sections.
        intro = re.split(r"\n\s*\*\*(?:Why|How to apply):\*\*", body, maxsplit=1)[0]
        intro = _strip(intro)
        why_t = _strip(why.group(1)) if why else ""
        how_t = _strip(how.group(1)) if how else ""

        context = why_t or fm.get("description", "") or intro
        question = fm.get("description", "") or (intro.split(".")[0] if intro else path.stem)
        decision_parts = [p for p in (intro, how_t) if p]
        decision = "\n\n".join(decision_parts) or fm.get("description", "")
        if not (context and question and decision):
            continue
        out.append({
            "context": context,
            "question": question,
            "decision": decision,
            "created": _mtime_iso(path),
            "source": "memory",
            "source_ref": f"memory/{path.name}",
            "tags": ["feedback"],
            "notes": [],
        })
    return out


# ---------------------------------------------------------------------------
# scopes.db access (Python sqlite3 — there is no sqlite3 CLI on the box)
# ---------------------------------------------------------------------------


def _open_scopes(scopes_db: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(scopes_db))
    conn.row_factory = sqlite3.Row
    return conn


# Throwaway demo/test operator posts that pollute the corpus — dropped up front.
_NOISE_RE = re.compile(
    r"^(v0 hello world|seed post|live post via REST|hello from|/\w+)\b", re.I
)


def scrape_operator_posts(
    scopes_db: str | Path, *, min_len: int = 40
) -> list[dict[str, Any]]:
    """Candidates from ``user:operator`` messages. A light gate drops test noise.

    The bulk of the operator posts in a dev DB are demo/throwaway ("hello
    world", "seed post", slash commands) or one-off task directives; the gate
    (drop ``_unowned`` scopes, slash commands, obvious test strings, and very
    short bodies) trims the worst so the curator reviews real steering.
    """
    conn = _open_scopes(scopes_db)
    try:
        rows = conn.execute(
            "SELECT id, owner_project, owner_scope, body, ts FROM scope_posts "
            "WHERE author='user:operator' AND kind='message' ORDER BY ts"
        ).fetchall()
    finally:
        conn.close()

    out: list[dict[str, Any]] = []
    for r in rows:
        body = (r["body"] or "").strip()
        if r["owner_project"] == "_unowned":
            continue
        if len(body) < min_len or _NOISE_RE.match(body):
            continue
        scope = f"{r['owner_project']}/{r['owner_scope']}"
        out.append({
            "context": f"Operator steering delivered to scope {scope}.",
            "question": "What did the operator direct here, and does it encode a durable preference?",
            "decision": body,
            "created": _ms_to_iso(r["ts"]),
            "source": "scope_post",
            "source_ref": f"scope_post/{r['id']}",
            "tags": ["operator", r["owner_project"]],
            "notes": [],
        })
    return out


# Markers that a journal decision records an explicit USER choice (vs the
# agent's own engineering call) — this is where the preference signal lives.
_USER_MARK_RE = re.compile(
    r"\buser('s)?\b|\boperator\b|per your\b|you (asked|wanted|said|prefer|chose|picked)",
    re.I,
)


def scrape_journal_decisions(
    scopes_db: str | Path,
    *,
    user_marked_only: bool = True,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Candidates from journal ``meta.decisions[]`` — agent-origin, lower confidence.

    Each ``decisions[]`` item becomes one candidate, tagged ``journal-sourced``
    and carrying a note that it is agent-origin (so a curator keeps the archive
    from drifting from "the user's preferences" into "the agent's past choices").
    ``user_marked_only`` (default) keeps only items whose text names the user —
    the subset that records a real user decision rather than an internal one.
    """
    conn = _open_scopes(scopes_db)
    try:
        rows = conn.execute(
            "SELECT id, owner_project, owner_scope, meta, ts FROM scope_posts "
            "WHERE kind='journal' ORDER BY ts"
        ).fetchall()
    finally:
        conn.close()

    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            meta = json.loads(r["meta"] or "{}")
        except (ValueError, TypeError):
            continue
        decisions = meta.get("decisions") or []
        if not isinstance(decisions, list):
            continue
        title = _strip(str(meta.get("title") or ""))
        scope = f"{r['owner_project']}/{r['owner_scope']}"
        created = _ms_to_iso(r["ts"])
        for i, d in enumerate(decisions):
            text = d if isinstance(d, str) else json.dumps(d)
            text = _strip(text)
            if not text:
                continue
            if user_marked_only and not _USER_MARK_RE.search(text):
                continue
            ctx = f"Session in scope {scope}" + (f" — {title}" if title else "") + "."
            out.append({
                "context": ctx,
                "question": "What was decided in this scope, and does it generalize as a preference?",
                "decision": text,
                "created": created,
                "source": "journal",
                "source_ref": f"scope_post/{r['id']}#dec{i}",
                "tags": ["journal-sourced", r["owner_project"]],
                "notes": [{
                    "body": "Agent-origin journal decision (auto-scraped); "
                            "lower confidence — confirm it reflects a durable user preference.",
                    "kind": "comment",
                }],
            })
            if limit is not None and len(out) >= limit:
                return out
    return out


# ---------------------------------------------------------------------------
# Combine
# ---------------------------------------------------------------------------


def build_candidates(
    *,
    memory_dir: str | Path,
    scopes_db: str | Path,
    include_operator: bool = True,
    include_journal: bool = True,
    journal_user_marked_only: bool = True,
    journal_limit: int | None = None,
) -> dict[str, Any]:
    """Concatenate every source into one staging-manifest ``{"decisions": [...]}``.

    Output is a *candidate* manifest for hand curation — not a load-ready file.
    """
    decisions: list[dict[str, Any]] = []
    decisions += scrape_memories(memory_dir)
    if include_operator:
        decisions += scrape_operator_posts(scopes_db)
    if include_journal:
        decisions += scrape_journal_decisions(
            scopes_db,
            user_marked_only=journal_user_marked_only,
            limit=journal_limit,
        )
    return {"decisions": decisions}
