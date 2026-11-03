"""Journal session ids, citations and reads (sessions.md R14, R15, R17)."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.journal]

ME_UUID = "12345678-9abc-def0-1234-56789abcdef0"
ME = "cc123456789abcdef0123456789abcdef0"
OTHER = "cc" + "f" * 32


@pytest.fixture()
def posts(journal_workspace, monkeypatch):
    from awm.journal.operations import posts as mod
    monkeypatch.setattr(mod.roster, "session_by_pid",
                        lambda pid: {"sessionId": ME_UUID} if pid == 42 else None)
    return mod


def _post(posts, kind="journal", meta=None, pid=42, body="debrief", scope="dev"):
    args = {"project": "awm", "scope": scope, "author": "agent:awm/dev", "body": body,
            "kind": kind, "meta": meta, "_caller_pid": pid}
    return posts._handle_post(args)["post"]


@pytest.mark.parametrize("raw,want", [
    (ME_UUID, ME), (ME_UUID.replace("-", ""), ME), (ME, ME), (ME.upper(), ME),
    ("ds" + "a" * 20, "ds" + "a" * 20),
    ("job00001", None), ("cc1234", None), ("xx" + "a" * 32, None), ("", None), (None, None),
])
def test_r9_matches_the_harness_form(raw, want):
    from awm.journal.sessions import r9
    assert r9(raw) == want


def test_a_journal_names_another_session_to_cover_it(posts):
    meta = _post(posts, meta={"session": OTHER})["meta"]
    assert meta == {"session": OTHER, "posted_by": ME}


def test_naming_your_own_session_adds_no_posted_by(posts):
    assert _post(posts, meta={"session": ME_UUID})["meta"] == {"session": ME}


def test_a_session_that_is_not_an_id_is_refused(posts):
    with pytest.raises(ValueError, match="not a session id"):
        _post(posts, meta={"session": "forged"})


def test_a_citation_points_at_a_journal_with_a_reason(posts):
    src = _post(posts, scope="other", pid=7, meta={"session": OTHER})
    out = _post(posts, meta={"cites": [{"journal": src["id"], "reason": " built the claim table "},
                                       {"journal": src["id"], "reason": "again"}]})
    assert out["meta"]["cites"] == [{"journal": src["id"], "reason": "built the claim table"}]


@pytest.mark.parametrize("cite,match", [
    ({"journal": "nope", "reason": "x"}, "not a journal post"),
    ({"journal": "ID", "reason": ""}, "one-line reason"),
    ({"journal": "ID", "reason": "two\nlines"}, "one-line reason"),
    ("ID", r"\{journal"),
])
def test_a_bad_citation_is_refused(posts, cite, match):
    src = _post(posts, scope="other", pid=7)
    cite = cite.replace("ID", src["id"]) if isinstance(cite, str) else \
        {**cite, "journal": cite["journal"].replace("ID", src["id"])}
    with pytest.raises(ValueError, match=match):
        _post(posts, meta={"cites": [cite]})


def test_a_message_is_not_citable_and_cannot_cite(posts):
    msg = _post(posts, kind="message", pid=7, body="chatter")
    with pytest.raises(ValueError, match="not a journal post"):
        _post(posts, meta={"cites": [{"journal": msg["id"], "reason": "x"}]})
    src = _post(posts, scope="other", pid=7)
    with pytest.raises(ValueError, match="only a journal"):
        _post(posts, kind="message", meta={"cites": [{"journal": src["id"], "reason": "x"}]})


def test_a_fetch_records_the_journals_a_session_read(posts):
    a = _post(posts, scope="other", pid=7, body="first")
    _post(posts, scope="other", pid=7, kind="message", body="chatter")
    _post(posts, scope="other", body="my own")
    posts._handle_fetch({"project": "awm", "scope": "other", "_caller_pid": 42})
    posts._handle_fetch({"post_id": a["id"], "_caller_pid": 42})
    out = posts._handle_reads({"_caller_pid": 42})
    assert out["session"] == ME
    assert [(r["journal"], r["times"], r["title"]) for r in out["reads"]] == [(a["id"], 2, "first")]


def test_a_fetch_without_a_session_or_from_a_peer_records_nothing(posts, monkeypatch):
    import awm.config as cfg
    monkeypatch.setattr(cfg, "peer_record",
                        lambda n: {"name": n, "relation": "foreign", "grants": ["journals"]})
    _post(posts, scope="other", pid=7)
    posts._handle_fetch({"project": "awm", "scope": "other", "_caller_pid": 7})
    posts._handle_fetch({"project": "awm", "scope": "other", "_caller_pid": 42}, "peer:capella")
    assert posts._handle_reads({"_caller_pid": 42})["reads"] == []
    with pytest.raises(PermissionError):
        posts._handle_reads({"session": ME}, "peer:capella")


def test_reads_lists_the_citations_a_session_already_made(posts):
    src = _post(posts, scope="other", pid=7)
    _post(posts, meta={"cites": [{"journal": src["id"], "reason": "the base"}]})
    out = posts._handle_reads({"session": ME_UUID})
    assert [(c["journal"], c["session"]) for c in out["cites"]] == [(src["id"], None)]
    named = _post(posts, scope="other", pid=7, meta={"session": OTHER})
    _post(posts, meta={"cites": [{"journal": named["id"], "reason": "the claim"}]})
    assert posts._handle_reads({"session": ME})["cites"][-1]["session"] == OTHER


def test_an_old_database_gains_the_reads_table(tmp_path, monkeypatch):
    import sqlite3
    import awm.persistence.databases as pdbs
    from awm.journal import dao
    monkeypatch.setattr(pdbs, "SERVICES_DIR", tmp_path)
    path = pdbs.service_db_path("journal")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(dao.SCHEMA_SQL + "CREATE TABLE schema_version (version INTEGER NOT NULL);"
                                        "INSERT INTO schema_version VALUES (1);")
    conn.close()
    monkeypatch.setattr(dao, "_initialized", False)
    dao.init()
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 2
    conn.execute("SELECT * FROM session_reads").fetchall()
