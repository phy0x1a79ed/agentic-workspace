"""The exact section: phrase matching, filters, $sort, the sections of an answer, and sections from elsewhere."""

from __future__ import annotations

import random

import pytest

from awm.search.lance_query import words
from awm.search.query import QueryError, parse

from .conftest import doc, every, ids, synced

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
        "j1": doc("Deploy notes", LOG, "2026-03-01T10:00:00Z", project="awm", tag=["journal"]),
        "j2": doc("Gateway port", "the gateway port moved to 8443 last week", "2026-05-01T10:00:00Z",
                  project="awm", tag=["message"]),
        "j3": doc("CONNECTIONREFUSEDERROR in caps", "an old CONNECTIONREFUSEDERROR log", "2026-06-01T10:00:00Z",
                  tag=["journal", "closed"]),
        "p1": doc("Ports", "prefer port 8443 for the gateway", "2026-04-01T10:00:00Z", tag=["precedent"]),
        "w1": doc("Ports again", "the gateway listens on port 8443", "2026-02-01T10:00:00Z")})
    await synced(archive, gateway)
    return archive


async def ask(arch, query, **kw):
    return await arch.query(parse({"query": query, **kw}))


def contains(text):
    return {"text": {"$contains": text}}


async def exact(arch, query, **kw):
    return set(ids((await ask(arch, query, **kw))["journals"]["exact"]))


async def test_a_phrase_finds_a_hash_a_path_and_an_error_line(loaded):
    for literal in ("3f9a2c1d7e", "/etc/awm/env", "[Errno 111] Connection refused", "AWM_GATEWAY_PORT=8443"):
        result = await ask(loaded, contains(literal))
        assert ids(result["journals"]["exact"]) == ["j1"], literal
        assert set(result["journals"]["exact"][0]) == {"id", "title", "ts"}
        assert "related" not in result["journals"]


async def test_a_phrase_crosses_punctuation_and_section_breaks(loaded):
    assert await exact(loaded, contains("3f9a2c1d7e.\n\n## Fix")) == {"j1"}
    assert await exact(loaded, contains("Connection-refused")) == {"j1"}


async def test_a_phrase_ignores_case(loaded):
    for literal in ("ConnectionRefusedError", "connectionrefusederror"):
        assert await exact(loaded, contains(literal)) == {"j1", "j3"}, literal


async def test_a_phrase_matches_whole_words_in_order(loaded):
    assert await exact(loaded, contains("3f9a")) == set()
    assert await exact(loaded, contains("8443 port")) == set()
    assert await exact(loaded, contains("Errno")) == {"j1"}


async def test_a_string_query_splits_by_phrase_into_exact_and_related(loaded):
    answer = (await ask(loaded, "port 8443"))["journals"]
    assert set(ids(answer["exact"])) == {"j1", "p1", "w1"}
    assert set(ids(answer["related"])) == {"j2", "j3"}


async def test_a_document_holding_only_some_of_the_words_is_not_exact(loaded):
    result = await ask(loaded, "gateway restart unit caps")
    assert result["journals"]["exact"] == []
    assert set(ids(result["journals"]["related"])) <= {"j1", "j2", "j3", "p1", "w1"}


@pytest.mark.parametrize("filter, expected", [
    ({"tag": "precedent"}, {"p1"}),
    ({"tag": "PRECEDENT"}, set()),
    ({"tag": "journal"}, {"j1", "j3"}),
    ({"tag": "jour*"}, {"j1", "j3"}),
    ({"tag": {"$in": ["message", "precedent"]}}, {"j2", "p1"}),
    ({"tag": {"$all": ["journal", "closed"]}}, {"j3"}),
    ({"tag": {"$all": ["journal", "message"]}}, set()),
    ({"tag": {"$nin": ["closed"]}}, {"j1", "j2", "p1", "w1"}),
    ({"tag": {"$ne": "journal"}}, {"j2", "p1", "w1"}),
    ({"$nor": [{"tag": "journal"}]}, {"j2", "p1", "w1"}),
    ({"$or": [{"tag": "message"}, {"tag": "closed"}]}, {"j2", "j3"}),
    ({"$and": [{"tag": "journal"}, {"tag": {"$nin": ["closed"]}}]}, {"j1"}),
    ({"project": {"$ne": "awm"}}, {"j3", "p1", "w1"}),
    ({"ts": {"$gte": "2026-04-01", "$lt": "2026-06-01"}}, {"j2", "p1"}),
    ({"ts": {"$gt": "2026-04-01T10:00:00Z"}}, {"j2", "j3"}),
])
async def test_filters_follow_mongodb_semantics(loaded, filter, expected):
    assert await exact(loaded, {"journals": filter}, max=50) == expected


@pytest.mark.parametrize("filter", [{"tag": ["journal", "closed"]}, {"tag": {"$regex": "jour"}},
                                    {"tag": {"$in": "journal"}}, {"nonesuch": 1}, {"text": "x"},
                                    {"$sort": "nonesuch"}, {"$and": [{"$sort": "ts"}]}])
def test_a_filter_that_cannot_be_read_is_refused(filter):
    with pytest.raises(QueryError):
        parse({"query": {"journals": filter}})


async def test_a_comparison_on_a_categorical_is_an_error_on_its_source(loaded):
    result = await ask(loaded, {"journals": {"tag": {"$gt": "a"}}})
    assert result["journals"]["exact"] == [] and "$gt" in result["journals"]["error"]


async def test_no_text_stops_at_max_and_sort_orders_by_a_field(loaded):
    result = await ask(loaded, {"journals": {}}, max=3)
    assert len(result["journals"]["exact"]) == 3 and "related" not in result["journals"]
    assert set(result["journals"]["exact"][0]) == {"id", "title", "ts"}
    newest = await ask(loaded, {"journals": {"$sort": "-ts"}}, max=3)
    assert ids(newest["journals"]["exact"]) == ["j3", "j2", "p1"]
    oldest = await ask(loaded, {"journals": {"$sort": "+ts", "project": "awm"}})
    assert ids(oldest["journals"]["exact"]) == ["j1", "j2"]


async def test_sort_on_content_orders_by_match_score(loaded):
    text = {"$or": [{"$contains": "gateway"}, {"$contains": "port"}]}
    best = await ask(loaded, {"text": text, "journals": {"$sort": "-text"}}, max=50)
    worst = await ask(loaded, {"text": text, "journals": {"$sort": "+text"}}, max=50)
    assert ids(best["journals"]["exact"]) == ids(worst["journals"]["exact"])[::-1]
    unscored = await ask(loaded, {"journals": {"$sort": "-text"}})
    assert unscored["journals"]["exact"] == [] and "needs a $contains" in unscored["journals"]["error"]


@pytest.mark.parametrize("text, expected", [
    ({"$and": [{"$contains": "gateway"}, {"$contains": "8443"}]}, {"j1", "j2", "p1", "w1"}),
    ({"$and": [{"$contains": "gateway"}, {"$contains": "LISTENS"}]}, {"w1"}),
    ({"$or": [{"$contains": "listens"}, {"$contains": "caps"}]}, {"j3", "w1"}),
    ({"$nor": [{"$contains": "gateway"}]}, {"j3"}),
    ({"$and": [{"$contains": "8443"}, {"$nor": [{"$contains": "port moved"}]}]}, {"j1", "p1", "w1"}),
    ({"$or": [{"$contains": "caps"}, {"$and": [{"$contains": "port"}, {"$contains": "prefer"}]}]}, {"j3", "p1"}),
    ({"$or": [{"$contains": "listens"}, {"$nor": [{"$contains": "gateway"}]}]}, {"j3", "w1"}),
])
async def test_contains_nests_under_and_or_nor(loaded, text, expected):
    assert await exact(loaded, {"text": text}, max=50) == expected


async def test_a_contains_and_a_filter_must_both_hold(loaded):
    assert await exact(loaded, {"text": {"$contains": "gateway"}, "journals": {"tag": "journal"}}) == {"j1"}


async def test_scope_covers_nested_scopes_and_globs(archive, gateway):
    gateway.plug("journals", {
        "a": doc("A", "x", "2026-01-01T00:00:00Z", project="awm", scope="redux"),
        "b": doc("B", "x", "2026-01-02T00:00:00Z", project="awm", scope="redux/search"),
        "c": doc("C", "x", "2026-01-03T00:00:00Z", project="awm", scope="redux-dev"),
        "d": doc("D", "x", "2026-01-04T00:00:00Z", project="other", scope="redux")})
    await synced(archive, gateway)
    nested = await ask(archive, {"journals": {"scope": "redux", "project": "awm", "$sort": "-ts"}})
    assert ids(nested["journals"]["exact"]) == ["b", "a"]
    assert await exact(archive, {"journals": {"scope": "redux*", "project": "awm"}}) == {"a", "b", "c"}
    assert await exact(archive, {"journals": {"scope": "redux/*"}}) == {"b"}
    assert await exact(archive, {"journals": {"scope": {"$ne": "redux"}}}) == {"c"}


async def test_replies_to_lists_the_posts_that_reply(archive, gateway):
    gateway.plug("journals", {
        "root": doc("Root", "question", "2026-01-01T00:00:00Z"),
        "r1": doc("Re", "answer", "2026-01-02T00:00:00Z", replies_to=["root"]),
        "r2": doc("Re2", "another", "2026-01-03T00:00:00Z", replies_to=["root", "r1"]),
        "r3": doc("Re3", "elsewhere", "2026-01-04T00:00:00Z", replies_to=["r1"])})
    await synced(archive, gateway)
    result = await ask(archive, {"journals": {"replies_to": "root", "$sort": "-ts"}})
    assert ids(result["journals"]["exact"]) == ["r2", "r1"]
    assert await exact(archive, {"text": {"$contains": "answer"}, "journals": {"replies_to": {"$in": ["root"]}}}) \
        == {"r1"}


async def test_about_under_or_gives_a_related_section_disjoint_from_exact(loaded):
    text = {"$or": [{"$about": "gateway port"}, {"$contains": "8443"}]}
    answer = (await ask(loaded, {"text": text}))["journals"]
    assert set(ids(answer["exact"])) == {"j1", "j2", "p1", "w1"}
    assert "related" in answer and ids(answer["related"]) == ["j3"]
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


async def test_about_with_a_sort_reorders_the_most_relevant(loaded):
    text = {"$about": "gateway port 8443"}
    top = ids((await ask(loaded, {"text": text}, max=3))["journals"]["exact"])
    sorted_top = (await ask(loaded, {"text": text, "journals": {"$sort": "-ts"}}, max=3))["journals"]["exact"]
    assert set(ids(sorted_top)) == set(top)
    assert [h["ts"] for h in sorted_top] == sorted((h["ts"] for h in sorted_top), reverse=True)


async def test_a_quiet_read_writes_nothing(loaded):
    again = await loaded.sweep("journals")
    assert again["upserted"] == 0 and again["deleted"] == 0


async def test_an_edit_replaces_what_a_phrase_finds(archive, gateway):
    feed = gateway.plug("journals", {"j1": doc("Notes", LOG)})
    await synced(archive, gateway)
    feed.docs["j1"] = doc("Notes", "replaced body with deadbeefcafe")
    assert (await archive.sweep("journals"))["upserted"] == 1
    assert (await ask(archive, contains("3f9a2c1d7e")))["journals"]["exact"] == []
    assert ids((await ask(archive, contains("deadbeefcafe")))["journals"]["exact"]) == ["j1"]


def _phrase_in(needle: list[str], field: str) -> bool:
    return f" {' '.join(needle)} " in f" {' '.join(words(field))} "


async def test_grep_parity_on_sampled_phrases(archive, gateway):
    rng = random.Random(7)
    vocab = ["tap", "flow", "sink", "gateway", "capella", "error", "path", "hash", "build", "merge"]
    docs = {}
    for i in range(40):
        body = []
        for _ in range(rng.randint(3, 12)):
            line = " ".join(rng.choice(vocab) for _ in range(rng.randint(4, 20)))
            body.append(f"{line} {rng.getrandbits(64):016x} /srv/{rng.choice(vocab)}/{i}.log")
            if rng.random() < 0.3:
                body.append("")
        docs[f"d{i:02d}"] = doc(f"Doc {i}", "\n".join(body))
    gateway.plug("journals", docs)
    await synced(archive, gateway)
    for _ in range(200):
        key = rng.choice(sorted(docs))
        tokens = words(docs[key]["text"])
        n = rng.randint(1, 6)
        at = rng.randint(0, len(tokens) - n)
        needle = tokens[at:at + n]
        found = await exact(archive, contains(" ".join(needle)), max=100)
        grep = {k for k, d in docs.items() if _phrase_in(needle, d["title"]) or _phrase_in(needle, d["text"])}
        assert key in grep and found == grep, needle


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


async def test_a_reply_in_another_shape_is_an_error_on_its_source(loaded, peers):
    peers.add("capella", lambda args: {"hits": [{"source": "journals", "id": "r1", "snippet": "from afar"}]})
    result = await ask(loaded, {"text": "zzzz", "journals@capella": {}})
    assert result["journals@capella"]["exact"] == [] and result["journals@capella"]["error"]




async def test_a_get_from_another_node_is_stamped_with_that_node(loaded, peers, monkeypatch):
    from awm.search import hub_adapter
    monkeypatch.setattr(hub_adapter, "ARCHIVE", loaded)
    monkeypatch.setattr(hub_adapter.privacy, "marks", lambda: [])
    peers.add("capella", lambda args: {"document": {"title": "far", "text": "body"}})
    got = await hub_adapter.HANDLERS["get"]({"source": "journals", "id": "r1", "node": "capella"}, None)
    assert got["document"]["origin"] == {"node": "capella"} and got["document"]["title"] == "far"
