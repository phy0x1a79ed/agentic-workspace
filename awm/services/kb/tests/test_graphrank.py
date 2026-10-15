"""PageRank for search hits: the global rank, a scope blended with its project, and hits not yet in the graph."""

import sqlite3

import pytest

from awm.kb import graphrank, hub_adapter

DDL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE node(nid INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL);
CREATE TABLE profile(name TEXT PRIMARY KEY, seeds INTEGER NOT NULL, floor REAL NOT NULL, mean REAL NOT NULL,
                     gmean REAL NOT NULL);
CREATE TABLE rank(profile TEXT NOT NULL, nid INTEGER NOT NULL, score REAL NOT NULL, PRIMARY KEY(profile, nid));
"""

NODES = ["paper:doi:10.1/at", "paper:doi:10.1/cas", "doc:journals:j1"]


def _graph(path, schema=graphrank.SCHEMA):
    db = sqlite3.connect(path)
    db.executescript(DDL)
    db.execute("INSERT INTO meta VALUES ('schema', ?)", (str(schema),))
    db.executemany("INSERT INTO node VALUES (?,?,?)", [(i, k, k.split(":")[0]) for i, k in enumerate(NODES)])
    db.executemany("INSERT INTO profile VALUES (?,?,?,?,?)", [
        ("global", 3, 0.1, 0.3, 0.3), ("scope:awm/kb", 20, 0.01, 0.5, 0.2), ("project:awm", 40, 0.02, 0.4, 0.25)])
    db.executemany("INSERT INTO rank VALUES (?,?,?)", [
        ("global", 0, 0.2), ("global", 1, 0.6), ("global", 2, 0.2),
        ("scope:awm/kb", 0, 0.9), ("scope:awm/kb", 2, 0.1),
        ("project:awm", 0, 0.5), ("project:awm", 1, 0.3)])
    db.commit()
    db.close()
    return path


HITS = [{"source": "kb", "id": "doi:10.1/at"}, {"source": "kb", "id": "doi:10.1/cas"},
        {"source": "journals", "id": "new", "project": "awm", "scope": "kb"}]


def test_without_a_context_the_global_rank_scores_and_a_new_post_takes_its_scopes_mean(tmp_path):
    ranks = graphrank.Ranks(_graph(tmp_path / "graph.db"))
    out = ranks.score(HITS)
    assert out == {"profile": "global", "scores": [0.2, 0.6, 0.2]}


def test_a_scope_blends_with_its_project_by_its_document_count(tmp_path):
    ranks = graphrank.Ranks(_graph(tmp_path / "graph.db"))
    out = ranks.score(HITS, "awm/kb")
    assert out["profile"] == "scope:awm/kb + project:awm"
    at, cas, new = out["scores"]
    assert at == pytest.approx(0.5 * 0.9 + 0.5 * 0.5)
    assert cas == pytest.approx(0.5 * 0.01 + 0.5 * 0.3)
    assert new == pytest.approx(0.5 * 0.5 + 0.5 * 0.4)
    assert ranks.score(HITS, "awm")["profile"] == "project:awm"
    assert ranks.score(HITS, "elsewhere/x")["profile"] == "global"


def test_no_graph_or_another_schema_scores_nothing(tmp_path):
    assert graphrank.Ranks(tmp_path / "none.db").score(HITS)["profile"] is None
    other = graphrank.Ranks(_graph(tmp_path / "old.db", schema=99))
    assert other.score(HITS) == {"profile": None, "scores": [None, None, None]}


def test_a_shipped_graph_replaces_the_open_one(tmp_path):
    path = _graph(tmp_path / "graph.db")
    ranks = graphrank.Ranks(path)
    assert ranks.score(HITS[:1])["scores"] == [0.2]
    fresh = _graph(tmp_path / "fresh.db")
    db = sqlite3.connect(fresh)
    db.execute("UPDATE rank SET score = 0.7 WHERE profile = 'global' AND nid = 0")
    db.commit()
    db.close()
    fresh.replace(path)
    assert ranks.score(HITS[:1])["scores"] == [0.7]


async def test_the_rank_verb_refuses_a_foreign_caller_and_bad_hits(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_adapter, "RANKS", graphrank.Ranks(_graph(tmp_path / "graph.db")))
    monkeypatch.setattr(hub_adapter, "_foreign", lambda as_: as_ == "peer:shaula")
    with pytest.raises(PermissionError):
        await hub_adapter.HANDLERS["rank"]({"hits": HITS}, as_="peer:shaula")
    with pytest.raises(ValueError):
        await hub_adapter.HANDLERS["rank"]({"hits": [{"source": "kb"}]})
    out = await hub_adapter.HANDLERS["rank"]({"hits": HITS, "context": "awm/kb"})
    assert len(out["scores"]) == 3
