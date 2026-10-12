"""The live archive query that backs the search service's `transcripts` source."""

from __future__ import annotations

import gzip
import tomllib
from pathlib import Path

import pytest

from awm.transcripts import layout, query, search


@pytest.fixture
def archive(tmp_path, monkeypatch):
    root = tmp_path / "archive"
    for project, sid, lines in [
        ("-home-awm", "s1", ['{"text":"we chose the Deep Kitchen Sink"}', '{"text":"unrelated"}']),
        ("-home-other", "s2", ['{"text":"the deep kitchen sink again (v2)"}']),
    ]:
        (root / project).mkdir(parents=True)
        with gzip.open(root / project / f"{sid}.jsonl.gz", "wt") as fh:
            fh.write("\n".join(lines) + "\n")
    monkeypatch.setattr(layout, "ARCHIVE", root)
    monkeypatch.setattr(search, "ARCHIVE", root)
    return root


def test_a_query_is_matched_as_words_ignoring_case(archive):
    out = query.hits("deep kitchen sink")
    assert sorted(h["id"] for h in out["hits"]) == ["s1:1", "s2:1"]
    assert out["truncated"] is False


def test_regex_characters_in_a_query_match_literally(archive):
    assert [h["id"] for h in query.hits("sink again (v2)")["hits"]] == ["s2:1"]
    assert query.hits(".*")["hits"] == []


def test_a_hit_has_the_shape_search_reads(archive):
    hit = query.hits("unrelated")["hits"][0]
    assert set(hit) == {"id", "title", "snippet", "project"}
    assert hit["id"] == "s1:2" and hit["project"] == "-home-awm" and "unrelated" in hit["snippet"]


def test_project_and_limit_narrow_it(archive):
    assert [h["id"] for h in query.hits("kitchen", project="other")["hits"]] == ["s2:1"]
    out = query.hits("kitchen", limit=1)
    assert len(out["hits"]) == 1 and out["truncated"] is True


def test_the_source_is_live_opt_in_and_the_service_is_off():
    import awm.transcripts
    toml = Path(awm.transcripts.__path__[0]).parents[1] / "service.toml"
    data = tomllib.loads(toml.read_text())
    assert data["tier"] == "off"
    assert data["archive"] == [{"name": "transcripts", "mode": "live", "query": "transcripts_query",
                                "optin": True}]


def test_the_query_verb_is_hidden_and_declares_every_filter_search_sends():
    from awm.transcripts.hub_adapter import API_MANIFEST, HANDLERS
    fn = next(f for f in API_MANIFEST["functions"] if f["name"] == "query")
    assert fn["surfaces"] == ["cli", "http"] and fn["effect"] == "read" and fn["tool"] == "transcripts_query"
    assert {p["name"] for p in fn["params"]} >= {"query", "project", "scope", "kind", "after", "before", "limit"}
    assert "query" in HANDLERS
