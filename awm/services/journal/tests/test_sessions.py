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
    monkeypatch.setattr(mod.caller, "session_of",
                        lambda pid: {"session_id": ME} if pid == 42 else None)
    return mod


def _post(posts, kind="journal", meta=None, pid=42, body="debrief", scope="dev", **extra):
    args = {"project": "awm", "scope": scope, "author": "agent:awm/dev", "body": body,
            "kind": kind, "meta": meta, "_caller_pid": pid, **extra}
    return posts._handle_post(args)["post"]


def test_a_journal_names_another_session_to_cover_it(posts):
    meta = _post(posts, meta={"session": OTHER})["meta"]
    assert meta == {"session": OTHER, "posted_by": ME}


def test_naming_your_own_session_adds_no_posted_by(posts):
    assert _post(posts, meta={"session": ME_UUID})["meta"] == {"session": ME}


def test_a_session_that_is_not_an_id_is_refused(posts):
    with pytest.raises(ValueError, match="not a session id"):
        _post(posts, meta={"session": "forged"})


def test_an_old_meta_cites_folds_into_reply_to_and_keeps_its_reasons(posts):
    src = _post(posts, scope="other", pid=7, meta={"session": OTHER})
    out = _post(posts, meta={"cites": [{"journal": src["id"], "reason": " built the claim table "},
                                       {"journal": src["id"], "reason": "again"}]})
    assert out["reply_to"] == [src["id"]] and "cites" not in out["meta"]
    assert out["meta"]["cite_reasons"] == {src["id"]: "again"}
    bare = _post(posts, meta={"cites": [{"journal": src["id"]}]})
    assert bare["reply_to"] == [src["id"]] and "cite_reasons" not in bare["meta"]


@pytest.mark.parametrize("cite,match", [
    ({"journal": "nope"}, "no post nope"),
    ("ID", r"\{journal"),
])
def test_a_bad_citation_is_refused(posts, cite, match):
    src = _post(posts, scope="other", pid=7)
    cite = cite.replace("ID", src["id"]) if isinstance(cite, str) else cite
    with pytest.raises(ValueError, match=match):
        _post(posts, meta={"cites": [cite]})


def test_a_message_is_not_citable_and_cannot_cite(posts):
    msg = _post(posts, kind="message", pid=7, body="chatter")
    with pytest.raises(ValueError, match="not a journal post"):
        _post(posts, reply_to=[msg["id"]])
    src = _post(posts, scope="other", pid=7)
    with pytest.raises(ValueError, match="only a debrief journal"):
        _post(posts, kind="message", meta={"cites": [{"journal": src["id"]}]})


def test_a_fetch_records_the_journals_a_session_read(posts):
    a = _post(posts, scope="other", pid=7, body="first")
    _post(posts, scope="other", pid=7, kind="message", body="chatter")
    _post(posts, scope="other", body="my own")
    posts._handle_fetch({"project": "awm", "scope": "other", "_caller_pid": 42})
    posts._handle_fetch({"post_id": a["id"], "_caller_pid": 42})
    out = posts._handle_reads({"_caller_pid": 42})
    assert out["session"] == ME
    assert [(r["journal"], r["times"], r["title"]) for r in out["reads"]] == [(a["id"], 2, "first")]


def test_a_read_another_service_notes_is_a_read(posts):
    """search get of a journal post (sessions.md R15)."""
    a = _post(posts, scope="other", pid=7, body="found by search")
    msg = _post(posts, scope="other", pid=7, kind="message", body="chatter")
    assert posts._handle_note_read({"post_id": a["id"], "_caller_pid": 42})["recorded"] == 1
    assert posts._handle_note_read({"post_id": msg["id"], "_caller_pid": 42})["recorded"] == 0
    assert posts._handle_note_read({"post_id": "nope", "_caller_pid": 42})["recorded"] == 0
    assert [r["journal"] for r in posts._handle_reads({"_caller_pid": 42})["reads"]] == [a["id"]]


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
    from tests.conftest import OLD_DDL
    conn.executescript(OLD_DDL)
    conn.close()
    monkeypatch.setattr(dao, "_initialized", False)
    dao.init()
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == dao.SCHEMA_VERSION
    conn.execute("SELECT * FROM session_reads").fetchall()
