"""Tests for awm.precedence.scrape — the T2 seeding scrapers.

Pure functions over on-disk memory files and a tiny throwaway scopes.db; no
embedding model or service DB needed, so nothing here skips.
"""

from __future__ import annotations

import sqlite3

import pytest

from awm.precedence import scrape

pytestmark = [pytest.mark.precedence]


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def test_ms_to_iso_treats_ts_as_milliseconds():
    # 1779318461990 ms == 2026-05-20 (scope_posts.ts is epoch ms, not seconds).
    assert scrape._ms_to_iso(1779318461990) == "2026-05-20"
    assert scrape._ms_to_iso(None) is None


# ---------------------------------------------------------------------------
# Memory scraper
# ---------------------------------------------------------------------------


def _write_feedback(dir_, name, body):
    p = dir_ / name
    p.write_text(body, encoding="utf-8")
    return p


def test_scrape_memories_splits_why_and_how(tmp_path):
    _write_feedback(tmp_path, "feedback_example.md", (
        "---\n"
        "name: feedback-example\n"
        'description: A one-line framing of the preference\n'
        "metadata:\n  type: feedback\n"
        "---\n\n"
        "The rule as stated up front.\n\n"
        "**Why:** because doing otherwise wastes the user's time.\n\n"
        "**How to apply:** do the concrete thing in this situation.\n\n"
        "Related: [[something]].\n"
    ))
    # a non-feedback file in the same dir must be ignored
    _write_feedback(tmp_path, "project_note.md", "not feedback\n")

    out = scrape.scrape_memories(tmp_path)
    assert len(out) == 1
    e = out[0]
    assert e["source"] == "memory"
    assert e["source_ref"] == "memory/feedback_example.md"
    assert "feedback" in e["tags"]
    assert e["created"] and len(e["created"]) == 10  # YYYY-MM-DD
    # Why -> context; description -> question; intro + How -> decision.
    assert "wastes the user's time" in e["context"]
    assert e["question"] == "A one-line framing of the preference"
    assert "The rule as stated up front." in e["decision"]
    assert "do the concrete thing" in e["decision"]
    # the trailing Related: line is not swept into the decision
    assert "Related" not in e["decision"]


# ---------------------------------------------------------------------------
# scopes.db scrapers (operator posts + journal decisions)
# ---------------------------------------------------------------------------


@pytest.fixture
def scopes_db(tmp_path):
    """A minimal scope_posts table with operator posts + a journal post."""
    path = tmp_path / "scopes.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE scope_posts (id TEXT PRIMARY KEY, owner_project TEXT, "
        "owner_scope TEXT, author TEXT, kind TEXT, body TEXT, meta TEXT, ts INTEGER)"
    )
    rows = [
        # test noise / demo — should be gated out
        ("p1", "_unowned", "isle", "user:operator", "message", "v0 hello world", "{}", 1779318461990),
        ("p2", "awm", "room-test", "user:operator", "message", "seed post", "{}", 1779318461990),
        ("p3", "awm", "web-ui", "user:operator", "slash", "/yolo", "{}", 1779318461990),
        ("p4", "awm", "web-ui", "user:operator", "message", "hi", "{}", 1779318461990),  # too short
        # a real steering post — should survive the gate
        ("p5", "metasmith", "dev", "user:operator", "message",
         "Pre-release validation pass: walk the tutorial end-to-end and flag every broken step.",
         "{}", 1779476902135),
        # a journal post: one user-marked decision, one internal decision
        ("p6", "awm", "web-ui", "agent:awm/web-ui", "journal", "",
         '{"title": "rooms scaffold", "decisions": ['
         '"Soft archive rather than destructive delete — user pick; matches no-data-loss posture", '
         '"Single-file SPA with hash-routed tabs to keep the no-build-step convention"]}',
         1779332165363),
    ]
    conn.executemany("INSERT INTO scope_posts VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return path


def test_scrape_operator_posts_gates_noise(scopes_db):
    out = scrape.scrape_operator_posts(scopes_db)
    # only the real steering post survives (noise, slash, _unowned, too-short dropped)
    assert len(out) == 1
    e = out[0]
    assert e["source"] == "scope_post"
    assert e["source_ref"] == "scope_post/p5"
    assert "Pre-release validation" in e["decision"]
    assert "operator" in e["tags"] and "metasmith" in e["tags"]
    assert e["created"] == "2026-05-22"


def test_scrape_journal_user_marked_only(scopes_db):
    marked = scrape.scrape_journal_decisions(scopes_db, user_marked_only=True)
    assert len(marked) == 1
    e = marked[0]
    assert e["source"] == "journal"
    assert e["source_ref"] == "scope_post/p6#dec0"
    assert "Soft archive" in e["decision"]
    assert "journal-sourced" in e["tags"]
    # agent-origin lower-confidence note is attached
    assert e["notes"] and e["notes"][0]["kind"] == "comment"
    assert "lower confidence" in e["notes"][0]["body"].lower()

    # without the gate, the internal (non-user) decision is included too
    allj = scrape.scrape_journal_decisions(scopes_db, user_marked_only=False)
    assert len(allj) == 2
    # limit caps the output
    assert len(scrape.scrape_journal_decisions(scopes_db, user_marked_only=False, limit=1)) == 1


def test_build_candidates_combines_sources(tmp_path, scopes_db):
    _write_feedback(tmp_path, "feedback_x.md", (
        "---\nname: x\ndescription: d\n---\n\nrule\n\n"
        "**Why:** w\n\n**How to apply:** h\n"
    ))
    manifest = scrape.build_candidates(memory_dir=tmp_path, scopes_db=scopes_db)
    sources = {d["source"] for d in manifest["decisions"]}
    assert sources == {"memory", "scope_post", "journal"}
    # toggles drop a source entirely
    only_mem = scrape.build_candidates(
        memory_dir=tmp_path, scopes_db=scopes_db,
        include_operator=False, include_journal=False,
    )
    assert {d["source"] for d in only_mem["decisions"]} == {"memory"}
