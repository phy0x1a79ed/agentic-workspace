"""The reference calls the signature was validated on, run against the kit's mock journal corpus.

The kit's kb, transcripts and ``@altair`` sources are not indexed on this node, so each call is run with
those keys dropped (see ``test_the_dropped_sources_*``); only the journals half of every task is asserted.
"""

from __future__ import annotations

import pytest

from awm.search.query import parse

from .conftest import doc, ids, section, synced

pytestmark = [pytest.mark.search]

# id, project, scope, tags, ts, title, body
_J = [
    ("J1", "awm", "redux-dev", ["debrief"], "2026-10-12T09:00:00Z", "Trigram budget cut to 3 per query", "The trigram budget now caps fan-out."),
    ("J2", "awm", "svc-search", ["issue"], "2026-10-20T14:00:00Z", "p95 regression after reindex", "p95 rose to 900 ms once the trigram budget ran out."),
    ("J3", "awm", "svc-search", ["issue", "closed"], "2026-10-05T11:00:00Z", "Timeout on peer fan-out", "A timeout fires when the peer is slow."),
    ("J4", "awm", "svc-journal", ["journal"], "2026-10-31T18:30:00Z", "Deploy notes", "Saw one timeout in the replay gate."),
    ("J5", "awm", "svc-journal", ["issue"], "2026-11-01T08:00:00Z", "Fetch fails again", "Another timeout on fetch."),
    ("J6", "awm", "svc-search/bench", ["journal"], "2026-10-15T10:00:00Z", "Benchmark run", "p95 steady at 120 ms."),
    ("J7", "awm", "svc-search", ["journal"], "2026-10-18T10:00:00Z", "Refactor ranking", "Search latency is flat after the refactor."),
    ("J8", "metasmith", "solver", ["issue"], "2026-10-10T10:00:00Z", "Solver stalls", "A timeout in the gurobi call."),
    ("J9", "awm", "redux-dev", ["issue"], "2026-09-28T10:00:00Z", "Search slow at scale", "p95 doubles at 10x load."),
    ("J10", "awm", "redux-dev", ["draft"], "2026-10-22T10:00:00Z", "HNSW against IVF, notes", "HNSW index build is slow."),
    ("J11", "awm", "svc-search", ["journal"], "2026-10-25T10:00:00Z", "HNSW recall check", "HNSW recall 0.97, FAISS baseline 0.95."),
    ("J12", "awm", "svc-search", ["journal"], "2025-03-02T10:00:00Z", "Adopt HNSW", "We move the vector index to HNSW."),
    ("J13", "awm", "svc-search", ["journal"], "2024-11-30T10:00:00Z", "First HNSW trial", "HNSW prototype works."),
    ("J14", "awm", "svc-kb", ["issue", "closed"], "2026-10-08T10:00:00Z", "p95 spike on ingest", "Fixed by batching."),
]
JOURNALS = {i: doc(title, body, ts, project=p, scope=s, tag=tags, kind="journal") for i, p, s, tags, ts, title, body in _J}
ALL = set(JOURNALS)

# The kit's reference calls, verbatim.
REF = [
    {"query": "trigram budget"},
    {"query": {"journals": {"project": "awm", "tag": {"$all": ["issue"], "$nin": ["closed"]}}}},
    {"query": {"text": {"$and": [{"$about": "search latency"}, {"$or": [{"$contains": "p95"}, {"$contains": "timeout"}]}]},
               "journals": {"project": "awm", "scope": "svc-*", "ts": {"$gte": "2026-10-01", "$lt": "2026-11-01"}}}},
    {"query": {"text": {"$about": "metagenome binning"}, "kb@altair": {"cites": "P1", "ts": {"$gte": "2024-01-01"}}}},
    {"query": {"text": {"$and": [{"$contains": "HNSW"}, {"$nor": [{"$contains": "FAISS"}]}]},
               "journals": {"ts": {"$gte": "2025-01-01"}, "tag": {"$ne": "draft"}},
               "journals@altair": {"ts": {"$gte": "2025-01-01"}, "tag": {"$ne": "draft"}},
               "transcripts": {"ts": {"$gte": "2025-01-01"}}, "kb@altair": {"ts": {"$gte": "2025-01-01"}}}},
]


async def ask(archive, gateway, call):
    gateway.plug("journals", dict(JOURNALS))
    await synced(archive, gateway)
    return await archive.query(parse(call))


def journals_only(call):
    """The reference call without its kb, transcripts and ``@node`` sources."""
    q = call["query"]
    if isinstance(q, str):
        return call
    return {**call, "query": {k: v for k, v in q.items() if k == "text" or k == "journals"}}


@pytest.mark.parametrize("n", range(5))
def test_every_reference_call_parses(n):
    assert parse(REF[n]).sources or parse(REF[n]).remote


def test_the_dropped_sources_are_not_served_here():
    req = parse(REF[4])
    assert [s.name for s in req.sources] == ["journals", "transcripts"]
    assert list(req.remote) == ["altair"]
    assert [s.name for s in parse(REF[3]).remote["altair"]] == ["kb@altair"]


async def test_task_1_a_phrase_finds_the_trigram_notes(archive, gateway):
    got = await ask(archive, gateway, journals_only(REF[0]))
    assert {"J1", "J2"} <= set(ids(section(got, "exact")))


async def test_task_2_open_issues_of_a_project(archive, gateway):
    got = await ask(archive, gateway, journals_only(REF[1]))
    assert sorted(ids(section(got, "exact"))) == ["J2", "J5", "J9"]
    assert "related" not in got["journals"]


async def test_task_2_sorted_newest_first(archive, gateway):
    call = journals_only(REF[1])
    call["query"]["journals"] = {**call["query"]["journals"], "$sort": "-ts"}
    got = await ask(archive, gateway, call)
    assert ids(section(got, "exact")) == ["J5", "J2", "J9"]


async def test_task_3_the_october_service_scopes_hold_every_must(archive, gateway):
    got = await ask(archive, gateway, journals_only(REF[2]))
    exact = set(ids(section(got, "exact")))
    assert {"J2", "J3", "J4", "J6", "J14"} <= exact
    assert exact <= {"J2", "J3", "J4", "J6", "J14"}
    assert "related" not in got["journals"]


async def test_task_5_hnsw_without_faiss_since_2025(archive, gateway):
    got = await ask(archive, gateway, journals_only(REF[4]))
    assert ids(section(got, "exact")) == ["J12"]
    assert "related" not in got["journals"]
