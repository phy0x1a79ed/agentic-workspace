"""The exact section, passages and their spans, kind lists, sections from elsewhere, and the move to schema 2."""

from __future__ import annotations

import random
import sqlite3

import pytest

from awm.search import passages, store
from awm.search.query import parse

from .conftest import doc, every, synced

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
        "j1": doc("Deploy notes", LOG, "2026-03-01T10:00:00Z", project="awm", kind="journal"),
        "j2": doc("Gateway port", "the gateway port moved to 8443 last week", "2026-05-01T10:00:00Z",
                  project="awm", kind="message"),
        "j3": doc("CONNECTIONREFUSEDERROR in caps", "an old CONNECTIONREFUSEDERROR log", "2026-06-01T10:00:00Z",
                  kind="journal")})
    gateway.plug("precedence", {"p1": doc("Ports", "prefer port 8443 for the gateway", kind="decision")})
    await synced(archive, gateway)
    return archive


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
        result = await loaded.query(parse({"query": literal}))
        top = result["exact"][0]
        assert (top["id"], top["match"]) == ("j1", "literal"), literal
        quote = top["passages"][0]
        start, end = quote["marks"][0]
        assert LOG[start:end].lower() == literal.lower()
        assert quote["text"] == LOG[quote["start"]:quote["end"]]


async def test_a_literal_across_two_passages_is_still_found(loaded):
    result = await loaded.query(parse({"query": "3f9a2c1d7e.\n\n## Fix"}))
    assert [h["id"] for h in result["exact"]] == ["j1"]
    quote = result["exact"][0]["passages"][0]
    assert "## Fix" in quote["text"] and quote["text"] == LOG[quote["start"]:quote["end"]]


async def test_a_case_sensitive_match_ranks_first_and_quotes_strip_one_pair(loaded):
    result = await loaded.query(parse({"query": '"ConnectionRefusedError"'}))
    assert [h["id"] for h in result["exact"][:2]] == ["j1", "j3"]
    assert result["exact"][1]["passages"][0]["field"] == "title"


async def test_a_short_literal_scans(loaded):
    result = await loaded.query(parse({"query": "3f", "sources": ["journals"]}))
    assert result["exact"][0]["id"] == "j1"


async def test_keywords_follow_literals_and_no_document_repeats_across_sections(loaded):
    result = await loaded.query(parse({"query": "gateway 8443"}))
    matches = [h["match"] for h in result["exact"]]
    assert matches == sorted(matches, key=lambda m: m != "literal")
    assert "keyword" in matches
    keys = [(h["source"], h["id"]) for h in every(result)]
    assert len(keys) == len(set(keys))


async def test_a_document_holding_only_some_of_the_words_is_related_not_exact(loaded):
    result = await loaded.query(parse({"query": "gateway restart unit caps"}))
    assert result["exact"] == []
    assert {h["id"] for h in result["related"]} >= {"j1", "j2", "j3", "p1"}
    both = await loaded.query(parse({"query": "restart unit"}))
    assert [(h["id"], h["match"]) for h in both["exact"]] == [("j1", "keyword")]


async def test_kind_takes_a_list(loaded):
    result = await loaded.query(parse({"query": "gateway", "kind": ["message", "decision"]}))
    assert {h["id"] for h in every(result)} == {"j2", "p1"}
    result = await loaded.query(parse({"query": "gateway", "kind": "journal,message"}))
    assert {h["id"] for h in every(result)} <= {"j1", "j2", "j3"}


async def test_a_quiet_pass_rewrites_nothing(loaded):
    again = await loaded.sweep("journals")
    assert again["documents"] == 3 and again["changed"] == 0


async def test_an_edit_recuts_the_passages(archive, gateway):
    exporter = gateway.plug("journals", {"j1": doc("Notes", LOG)})
    await synced(archive, gateway)
    exporter.docs["j1"] = doc("Notes", "replaced body with deadbeefcafe")
    assert (await archive.sweep("journals"))["changed"] == 1
    assert (await archive.query(parse({"query": "3f9a2c1d7e"})))["exact"] == []
    assert (await archive.query(parse({"query": "deadbeefcafe"})))["exact"][0]["id"] == "j1"
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
        result = await archive.query(parse({"query": needle, "limit": 100}))
        assert key in {h["id"] for h in result["exact"] if h["match"] == "literal"}, needle


# -- sections from elsewhere -----------------------------------------------


async def test_a_live_source_answering_in_sections_keeps_its_passages(loaded, gateway):
    gateway.plug_live("kb", lambda args: {
        "exact": [{"id": "doi:1", "title": "A paper", "passages": [
            {"text": "a quoted line", "start": 10, "end": 23, "section": "Methods", "marks": [[10, 23]],
             "injected": True}]}],
        "graph": [{"id": "doi:2", "title": "Linked paper"}]})
    await loaded.refresh()
    result = await loaded.query(parse({"query": "quoted line", "sources": ["kb"]}))
    assert [h["id"] for h in result["exact"]] == ["doi:1"]
    assert result["exact"][0]["passages"] == [{"text": "a quoted line", "start": 10, "end": 23,
                                               "section": "Methods", "marks": [[10, 23]]}]
    assert [h["id"] for h in result["graph"]] == ["doi:2"]


async def test_a_peer_on_an_older_release_answers_in_related(loaded, peers):
    peers.add("capella", lambda args: {"hits": [{"source": "journals", "id": "r1", "snippet": "from afar"}]})
    result = await loaded.query(parse({"query": "zzzz", "peers": "capella"}))
    assert [h["id"] for h in result["related"]] == ["r1"]
    assert result["related"][0]["passages"] == [{"text": "from afar"}]


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
