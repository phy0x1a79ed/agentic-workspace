"""The exact section, passages and their spans, filters, the sections of an answer, sections from elsewhere, and the move to schema 2."""

from __future__ import annotations

import random
import sqlite3

import pytest

from awm.search import passages, store
from awm.search.query import QueryError, parse

from .conftest import doc, every, ids, section, synced

pytestmark = [pytest.mark.search]

LOG = """# Deploy notes

The gateway restart on capella failed with
`ConnectionRefusedError: [Errno 111] Connection refused` after commit 3f9a2c1d7e.

## Fix

Edit /etc/awm/env and set AWM_GATEWAY_PORT=8443, then restart the unit.
"""


@pytest.fixture()
async def loaded(archive, gateway):
    gateway.plug("journals", {
        "j1": doc("Deploy notes", LOG, "2026-03-01T10:00:00Z", project="awm", tags=["journal"]),
        "j2": doc("Gateway port", "the gateway port moved to 8443 last week", "2026-05-01T10:00:00Z",
                  project="awm", tags=["message"]),
        "j3": doc("CONNECTIONREFUSEDERROR in caps", "an old CONNECTIONREFUSEDERROR log", "2026-06-01T10:00:00Z",
                  tags=["journal", "closed"]),
        "p1": doc("Ports", "prefer port 8443 for the gateway", "2026-04-01T10:00:00Z", tags=["Precedent"]),
        "w1": doc("Ports again", "the gateway listens on port 8443", "2026-02-01T10:00:00Z")})
    await synced(archive, gateway)
    return archive


async def ask(arch, query, **kw):
    return await arch.query(parse({"query": query, **kw}))


def contains(text):
    return {"text": {"$contains": text}}



def test_passages_are_exact_spans_with_their_sections():
    cut = passages.split(LOG)
    assert [p.section for p in cut] == ["Deploy notes", "Fix"]
    for p in cut:
        assert LOG[p.start:p.end] == LOG[p.start:p.end].strip()
    assert "3f9a2c1d7e" in LOG[cut[0].start:cut[0].end]


def test_a_long_block_is_cut_under_the_cap_at_sentence_ends():
    text = " ".join(f"Sentence number {i} says something about tap flow." for i in range(80))
    text += " " + "x" * 2000
    cut = passages.split(text)
    assert all(p.end - p.start <= passages.MAX_CHARS for p in cut)
    assert cut[0].end - cut[0].start > passages.MAX_CHARS // 2
    assert text[cut[0].start:cut[0].end].endswith(".")
    covered = "".join(text[p.start:p.end] for p in cut)
    assert covered.replace(" ", "") == text.replace(" ", "")


async def test_a_literal_finds_a_hash_a_path_and_an_error_line(loaded):
    for literal in ("3f9a2c1d7e", "/etc/awm/env", "[Errno 111] Connection refused", "PORT=84"):
        result = await ask(loaded, contains(literal))
        assert ids(result["journals"]["exact"]) == ["j1"], literal
        assert set(result["journals"]["exact"][0]) == {"id", "title", "ts"}
        assert "related" not in result["journals"]


async def test_a_literal_across_two_passages_is_still_found(loaded):
    result = await ask(loaded, contains("3f9a2c1d7e.\n\n## Fix"))
    assert ids(result["journals"]["exact"]) == ["j1"]


async def test_a_contains_ignores_case_and_lists_the_newest_first(loaded):
    for literal in ("ConnectionRefusedError", "connectionrefusederror"):
        result = await ask(loaded, contains(literal))
        assert ids(result["journals"]["exact"]) == ["j3", "j1"], literal


async def test_a_short_literal_scans(loaded):
    result = await ask(loaded, contains("3f"))
    assert ids(result["journals"]["exact"]) == ["j1"]


async def test_a_string_query_splits_by_substring_into_exact_and_related(loaded):
    result = await ask(loaded, "port 8443")
    answer = result["journals"]
    assert set(ids(answer["exact"])) == {"p1", "w1"}
    assert not set(ids(answer["exact"])) & set(ids(answer["related"]))
    assert set(ids(answer["related"])) <= {"j1", "j2", "j3"}


async def test_a_document_holding_only_some_of_the_words_is_not_exact(loaded):
    result = await ask(loaded, "gateway restart unit caps")
    assert result["journals"]["exact"] == []
    assert set(ids(result["journals"]["related"])) <= {"j1", "j2", "j3", "p1", "w1"}


@pytest.mark.parametrize("filter, expected", [
    ({"tag": "precedent"}, {"p1"}),
    ({"tag": "PRECEDENT"}, {"p1"}),
    ({"tag": "journal"}, {"j1", "j3"}),
    ({"tag": {"$in": ["message", "precedent"]}}, {"j2", "p1"}),
    ({"tag": {"$all": ["journal", "closed"]}}, {"j3"}),
    ({"tag": {"$all": ["journal", "message"]}}, set()),
    ({"tag": {"$nin": ["closed"]}}, {"j1", "j2", "p1", "w1"}),
    ({"tag": {"$ne": "journal"}}, {"j2", "p1", "w1"}),
    ({"$nor": [{"tag": "journal"}]}, {"j2", "p1", "w1"}),
    ({"$or": [{"tag": "message"}, {"tag": "closed"}]}, {"j2", "j3"}),
    ({"$and": [{"tag": "journal"}, {"tag": {"$nin": ["closed"]}}]}, {"j1"}),
])
async def test_tags_follow_mongodb_array_semantics(loaded, filter, expected):
    result = await ask(loaded, {"journals": filter}, max=50)
    assert set(ids(result["journals"]["exact"])) == expected


@pytest.mark.parametrize("filter", [{"tag": ["journal", "closed"]}, {"tag": {"$regex": "jour"}},
                                    {"tag": {"$in": "journal"}}, {"nonesuch": 1}])
def test_a_filter_that_cannot_be_read_is_refused(filter):
    with pytest.raises(QueryError):
        parse({"query": {"journals": filter}})


async def test_no_text_lists_newest_first_and_stops_at_max(loaded):
    result = await ask(loaded, {"journals": {}}, max=3)
    assert ids(result["journals"]["exact"]) == ["j3", "j2", "p1"]
    assert "related" not in result["journals"]
    assert set(result["journals"]["exact"][0]) == {"id", "title", "ts"}
    assert len(ids((await ask(loaded, {"journals": {"project": "awm"}}))["journals"]["exact"])) == 2


@pytest.mark.parametrize("text, expected", [
    ({"$and": [{"$contains": "gateway"}, {"$contains": "8443"}]}, ["j2", "p1", "j1", "w1"]),
    ({"$and": [{"$contains": "gateway"}, {"$contains": "LISTENS"}]}, ["w1"]),
    ({"$or": [{"$contains": "listens"}, {"$contains": "caps"}]}, ["j3", "w1"]),
    ({"$nor": [{"$contains": "gateway"}]}, ["j3"]),
    ({"$and": [{"$contains": "8443"}, {"$nor": [{"$contains": "port moved"}]}]}, ["p1", "j1", "w1"]),
    ({"$or": [{"$contains": "caps"}, {"$and": [{"$contains": "port"}, {"$contains": "prefer"}]}]}, ["j3", "p1"]),
])
async def test_contains_nests_under_and_or_nor(loaded, text, expected):
    result = await ask(loaded, {"text": text}, max=50)
    assert ids(result["journals"]["exact"]) == expected


async def test_a_contains_and_a_filter_must_both_hold(loaded):
    result = await ask(loaded, {"text": {"$contains": "gateway"}, "journals": {"tag": "journal"}})
    assert ids(result["journals"]["exact"]) == ["j1"]


async def test_scope_covers_nested_scopes_and_globs(archive, gateway):
    gateway.plug("journals", {
        "a": doc("A", "x", "2026-01-01T00:00:00Z", project="awm", scope="redux"),
        "b": doc("B", "x", "2026-01-02T00:00:00Z", project="awm", scope="redux/search"),
        "c": doc("C", "x", "2026-01-03T00:00:00Z", project="awm", scope="redux-dev"),
        "d": doc("D", "x", "2026-01-04T00:00:00Z", project="other", scope="redux")})
    await synced(archive, gateway)
    nested = await ask(archive, {"journals": {"scope": "redux", "project": "awm"}})
    assert ids(nested["journals"]["exact"]) == ["b", "a"]
    glob = await ask(archive, {"journals": {"scope": "redux*", "project": "awm"}})
    assert ids(glob["journals"]["exact"]) == ["c", "b", "a"]
    assert ids((await ask(archive, {"journals": {"scope": "redux/*"}}))["journals"]["exact"]) == ["b"]


async def test_replies_to_lists_the_posts_that_reply(archive, gateway):
    gateway.plug("journals", {
        "root": doc("Root", "question", "2026-01-01T00:00:00Z"),
        "r1": doc("Re", "answer", "2026-01-02T00:00:00Z", reply_to=["root"]),
        "r2": doc("Re2", "another", "2026-01-03T00:00:00Z", reply_to=["root", "r1"]),
        "r3": doc("Re3", "elsewhere", "2026-01-04T00:00:00Z", reply_to=["r1"])})
    await synced(archive, gateway)
    result = await ask(archive, {"journals": {"replies_to": "root"}})
    assert ids(result["journals"]["exact"]) == ["r2", "r1"]
    result = await ask(archive, {"text": {"$contains": "answer"}, "journals": {"replies_to": {"$in": ["root"]}}})
    assert ids(result["journals"]["exact"]) == ["r1"]


async def test_about_under_or_gives_a_related_section_disjoint_from_exact(loaded):
    text = {"$or": [{"$about": "gateway port"}, {"$contains": "8443"}]}
    answer = (await ask(loaded, {"text": text}))["journals"]
    assert set(ids(answer["exact"])) == {"j1", "j2", "p1", "w1"}
    assert "related" in answer and ids(answer["related"]) == ["j3"]
    assert not set(ids(answer["exact"])) & set(ids(answer["related"]))
    narrowed = (await ask(loaded, {"text": text, "journals": {"tag": "journal"}}))["journals"]
    assert ids(narrowed["exact"]) == ["j1"] and ids(narrowed["related"]) == ["j3"]


async def test_about_under_and_gives_no_related_section(loaded):
    text = {"$and": [{"$about": "gateway port"}, {"$contains": "8443"}]}
    answer = (await ask(loaded, {"text": text}))["journals"]
    assert set(ids(answer["exact"])) == {"j1", "j2", "p1", "w1"} and "related" not in answer


async def test_a_lone_about_ranks_everything_into_exact(loaded):
    for text in ({"$about": "gateway port 8443"}, {"$or": [{"$about": "gateway port 8443"}]}):
        answer = (await ask(loaded, {"text": text}, max=50))["journals"]
        assert set(ids(answer["exact"])) == {"j1", "j2", "j3", "p1", "w1"} and "related" not in answer
    assert len((await ask(loaded, {"text": {"$about": "gateway port 8443"}}, max=2))["journals"]["exact"]) == 2


async def test_a_quiet_pass_rewrites_nothing(loaded):
    again = await loaded.sweep("journals")
    assert again["documents"] == 5 and again["changed"] == 0


async def test_an_edit_recuts_the_passages(archive, gateway):
    exporter = gateway.plug("journals", {"j1": doc("Notes", LOG)})
    await synced(archive, gateway)
    exporter.docs["j1"] = doc("Notes", "replaced body with deadbeefcafe")
    assert (await archive.sweep("journals"))["changed"] == 1
    assert (await ask(archive, contains("3f9a2c1d7e")))["journals"]["exact"] == []
    assert ids((await ask(archive, contains("deadbeefcafe")))["journals"]["exact"]) == ["j1"]
    conn = store.connect()
    try:
        assert {r[0] for r in conn.execute("SELECT text FROM passages")} == {"Notes", "replaced body with deadbeefcafe"}
    finally:
        conn.close()


async def test_grep_parity_on_sampled_substrings(archive, gateway):
    rng = random.Random(7)
    words = ["tap", "flow", "sink", "gateway", "capella", "error", "path", "hash", "build", "merge"]
    docs = {}
    for i in range(40):
        body = []
        for _ in range(rng.randint(3, 12)):
            line = " ".join(rng.choice(words) for _ in range(rng.randint(4, 20)))
            body.append(f"{line} {rng.getrandbits(64):016x} /srv/{rng.choice(words)}/{i}.log")
            if rng.random() < 0.3:
                body.append("")
        docs[f"d{i:02d}"] = doc(f"Doc {i}", "\n".join(body))
    gateway.plug("journals", docs)
    await synced(archive, gateway)
    for _ in range(200):
        key = rng.choice(sorted(docs))
        text = docs[key]["text"]
        n = rng.randint(3, 40)
        at = rng.randint(0, len(text) - n)
        needle = text[at:at + n]
        if not needle.strip() or needle != needle.strip():
            continue
        result = await ask(archive, contains(needle), max=100)
        grep = {k for k, d in docs.items() if needle.lower() in f"{d['title']} {d['text']}".lower()}
        assert key in grep and set(ids(result["journals"]["exact"])) == grep, needle


# -- sections from elsewhere -----------------------------------------------


async def test_a_live_source_answering_in_sections_has_its_passages_dropped(loaded, gateway):
    gateway.plug_live("kb", lambda args: {
        "exact": [{"id": "doi:1", "title": "A paper", "passages": [
            {"text": "a quoted line", "start": 10, "end": 23, "section": "Methods", "marks": [[10, 23]],
             "injected": True}]}],
        "graph": [{"id": "doi:2", "title": "Linked paper"}]})  # a retired section, dropped
    await loaded.refresh()
    result = await ask(loaded, {"text": "quoted line", "kb": {}})
    assert ids(result["kb"]["exact"]) == ["doi:1"]
    assert set(result["kb"]["exact"][0]) == {"id", "title", "ts"}
    assert "doi:2" not in ids(every(result))


async def test_a_failing_live_source_reports_its_error_and_the_rest_answer(loaded, gateway):
    gateway.plug_live("kb", lambda args: RuntimeError("kb is down"))
    await loaded.refresh()
    result = await ask(loaded, {"text": "port", "kb": {}, "journals": {}})
    assert result["kb"]["exact"] == [] and "kb is down" in result["kb"]["error"]
    assert "error" not in result["journals"] and result["journals"]["exact"]


async def test_a_peer_answers_in_sections_and_is_asked_for_its_own_sources(loaded, peers):
    peers.add("capella", lambda args: {"journals": {"exact": [
        {"id": "r1", "title": "from afar", "ts": "2026-01-01T00:00:00Z", "passages": ["injected"], "extra": 1}],
        "related": []}})
    result = await ask(loaded, {"text": "zzzz", "journals@capella": {}})
    assert result["journals@capella"]["exact"] == [{"id": "r1", "title": "from afar", "ts": "2026-01-01T00:00:00Z"}]
    assert peers.calls[0][2] == {"query": {"text": "zzzz", "journals": {}}, "max": 10}


async def test_a_peer_on_an_older_release_is_an_error_on_its_source(loaded, peers):
    peers.add("capella", lambda args: {"hits": [{"source": "journals", "id": "r1", "snippet": "from afar"}]})
    result = await ask(loaded, {"text": "zzzz", "journals@capella": {}})
    assert result["journals@capella"]["exact"] == [] and result["journals@capella"]["error"]


async def test_a_mixed_fleet_fails_only_the_old_peers_sources(loaded, peers):
    peers.add("capella", lambda args: {"query": "zzzz", "sources": {"journals": {"status": "ok"}},
                                       "exact": [{"source": "journals", "id": "r1", "title": "old"}],
                                       "related": [], "degraded": False})
    peers.add("lyra", lambda args: RuntimeError("HTTP 422: unknown argument 'max'"))
    peers.add("deneb", lambda args: {"journals": {"exact": [
        {"id": "d1", "title": "new", "ts": "2026-01-01T00:00:00Z"}]}})
    result = await ask(loaded, {"text": "sink", "journals": {}, "journals@capella": {},
                                "journals@lyra": {}, "journals@deneb": {}})
    assert "error" not in result["journals"] and every({"journals": result["journals"]})
    assert result["journals@capella"] == {"exact": [], "error": "its release answers the old search signature"}
    assert result["journals@lyra"]["exact"] == [] and "unknown argument" in result["journals@lyra"]["error"]
    assert ids(result["journals@deneb"]["exact"]) == ["d1"]


# -- schema 2 --------------------------------------------------------------


def test_a_version_1_database_moves_to_the_current_schema(tmp_path, monkeypatch):
    services_dir = tmp_path / "services"
    (services_dir / "search").mkdir(parents=True)
    import awm.persistence.databases as pdbs
    monkeypatch.setattr(pdbs, "SERVICES_DIR", services_dir)
    monkeypatch.setattr(store, "_initialized", False)
    path = pdbs.service_db_path("search")
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version VALUES (1);
        CREATE TABLE sources (name TEXT PRIMARY KEY, service TEXT NOT NULL, mode TEXT NOT NULL,
            spec TEXT NOT NULL, present INTEGER NOT NULL DEFAULT 1, synced_at REAL, attempt_at REAL,
            last_error TEXT, first_seen REAL NOT NULL, last_seen REAL NOT NULL);
        CREATE TABLE documents (key TEXT PRIMARY KEY, source TEXT NOT NULL, doc_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '', project TEXT NOT NULL DEFAULT '',
            scope TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT '', ts TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '');
        CREATE INDEX idx_documents_source ON documents(source, doc_id);
        CREATE INDEX idx_documents_filter ON documents(source, project, scope, kind, ts);
        INSERT INTO documents (key, source, doc_id, title, text) VALUES
            ('journals:j1', 'journals', 'j1', 'Old post', 'kept across the move: c0ffee42');
    """)
    old.close()
    store.init()
    conn = store.connect()
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == store.SCHEMA_VERSION
        assert conn.execute("SELECT count(*) FROM judgements").fetchone()[0] == 0
        hit = conn.execute("SELECT d.doc_id FROM documents_exact JOIN documents d ON d.rid = documents_exact.rowid"
                           " WHERE documents_exact MATCH '\"c0ffee42\"'").fetchall()
        assert [r[0] for r in hit] == ["j1"]
        assert conn.execute("SELECT COUNT(*) FROM passages WHERE key='journals:j1'").fetchone()[0] == 2
    finally:
        conn.close()
