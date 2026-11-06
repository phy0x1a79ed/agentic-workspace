"""Citations as a journal's reply_to list, and the lineage walk (sessions.md R13, R14, R16)."""

from __future__ import annotations

import sqlite3

import pytest

pytestmark = [pytest.mark.journal]

S1 = "cc" + "1" * 32
S2 = "cc" + "2" * 32
S3 = "cc" + "3" * 32


def _post(**args):
    from awm.journal.operations.posts import POST_HANDLERS
    args.setdefault("author", "agent:awm/dev")
    args.setdefault("body", "text")
    return POST_HANDLERS["post"](args, None)["post"]


def _journal(session, scope="dev", reply_to=None):
    return _post(project="awm", scope=scope, tags=["journal"], meta={"session": session},
                 reply_to=reply_to, body=f"debrief of {session}")


def test_a_debrief_journal_cites_by_its_reply_to_list_and_stays_in_its_scope(journal_workspace):
    a = _journal(S1, scope="one")
    b = _journal(S2, scope="two")
    c = _journal(S3, scope="dev", reply_to=[a["id"], b["id"]])
    assert (c["project"], c["scope"]) == ("awm", "dev") and c["reply_to"] == [a["id"], b["id"]]
    assert "thread" not in c


def test_a_citation_must_name_a_journal(journal_workspace):
    msg = _post(project="awm", scope="dev", body="chatter")
    with pytest.raises(ValueError, match="not a journal post"):
        _journal(S1, reply_to=[msg["id"]])


def test_a_comment_answers_one_post(journal_workspace):
    a = _post(project="awm", scope="dev", body="one")
    b = _post(project="awm", scope="dev", body="two")
    with pytest.raises(ValueError, match="answers one post"):
        _post(reply_to=[a["id"], b["id"]], body="both")


def test_an_old_meta_cites_folds_into_reply_to(journal_workspace):
    a = _journal(S1)
    c = _post(project="awm", scope="dev", tags=["journal"],
              meta={"session": S2, "cites": [{"journal": a["id"], "reason": "the race fix"}]})
    assert c["reply_to"] == [a["id"]]


def test_lineage_walks_parents_and_citations_forward(journal_workspace):
    from awm.journal import lineage
    from awm.journal.operations.lineage import LINEAGE_HANDLERS
    assert LINEAGE_HANDLERS["session_parent"]({"session_id": S3, "parent": S2}, None)["recorded"]
    assert not lineage.set_parent(S3, S1)  # the first link wins
    a = _journal(S1)
    _journal(S3, reply_to=[a["id"]])
    out = LINEAGE_HANDLERS["lineage"]({"session": S3}, None)
    edges = [(e["from"], e["kind"], e["to"]) for e in out["edges"]]
    assert edges == [(S3, "parent", S2), (S3, "cite", a["id"])]
    assert lineage.walk(S2)["edges"] == []  # forward only: no edge back to S3


def test_reportings_parent_links_import_once(journal_workspace, tmp_path):
    from awm.journal import lineage
    src = tmp_path / "work.db"
    db = sqlite3.connect(src)
    db.execute("CREATE TABLE session_parents (session TEXT PRIMARY KEY, parent TEXT NOT NULL,"
               " how TEXT NOT NULL, node TEXT NOT NULL DEFAULT '', at REAL NOT NULL)")
    db.execute("INSERT INTO session_parents VALUES (?, ?, 'clear', 'mizar', 5.0)", (S2, S1))
    db.commit()
    db.close()
    assert lineage.import_parents(src)["recorded"] == 1
    assert lineage.import_parents(src)["recorded"] == 0
    assert lineage.parent_of(S2)["how"] == "clear"
