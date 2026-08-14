"""Tests for awm.precedence — decision-archive search + reputation + curation.

The embedding-backed paths (add/embed/fielded semantic) require the workspace
embedding stack (sentence-transformers + sqlite-vec); those tests skip if it's
absent. The manifest surface split, the pure usefulness math, tag/keyword paths,
votes, notes, and the supersede lifecycle run without any model.
"""

from __future__ import annotations

import importlib.util
import json

import pytest

pytestmark = [pytest.mark.precedence]

_HAS_EMBED = (
    importlib.util.find_spec("sentence_transformers") is not None
    and importlib.util.find_spec("sqlite_vec") is not None
)
needs_embed = pytest.mark.skipif(not _HAS_EMBED, reason="embedding stack not installed")


# ---------------------------------------------------------------------------
# Fixtures — isolated service DB in a temp dir (mirrors the writing pattern)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_precedence_db(tmp_path, monkeypatch):
    services_dir = tmp_path / "services"
    services_dir.mkdir()
    monkeypatch.setenv("AWM_WORKSPACE", str(tmp_path))
    import awm.persistence.databases as dbmod
    monkeypatch.setattr(dbmod, "SERVICES_DIR", services_dir, raising=False)
    import awm.precedence.dao as daomod
    monkeypatch.setattr(daomod, "_initialized", False)
    daomod.init()
    yield tmp_path


@pytest.fixture
def conn():
    from awm.precedence import dao
    c = dao.connect()
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Manifest surface split (no DB / model needed)
# ---------------------------------------------------------------------------


def test_manifest_surface_split():
    from awm.precedence.hub_adapter import API_MANIFEST, HANDLERS

    fns = {f["name"]: f for f in API_MANIFEST["functions"]}
    reads = {"search", "get", "stats", "add", "note", "vote"}
    curation = {"edit", "supersede", "remove", "merge", "import", "embed"}
    assert reads | curation == set(fns)
    # read/contribute verbs omit surfaces (default all three)
    for name in reads:
        assert "surfaces" not in fns[name], name
    # curation verbs are cli/http only — kept off the MCP surface
    for name in curation:
        assert fns[name]["surfaces"] == ["cli", "http"], name
    # every handler is wired, and every function has a distinct precedence_<verb> tool
    assert set(HANDLERS) == set(fns)
    tools = [f["tool"] for f in fns.values()]
    assert len(tools) == len(set(tools))
    for f in fns.values():
        assert f["tool"] == f"precedence_{f['name']}"
    # long-runners declare a timeout
    for name in ("import", "embed"):
        assert fns[name].get("timeout")


# ---------------------------------------------------------------------------
# Pure usefulness math (no DB / model needed)
# ---------------------------------------------------------------------------


def test_usefulness_relevance_primary():
    from awm.precedence import config
    hi = config.usefulness(relevance=0.9, upvotes=0, downvotes=0, seen_count=5,
                           age_days=100, explore=0.0)
    lo = config.usefulness(relevance=0.2, upvotes=0, downvotes=0, seen_count=5,
                           age_days=100, explore=0.0)
    assert hi["score"] > lo["score"]


def test_usefulness_reputation_breaks_tie():
    from awm.precedence import config
    up = config.usefulness(relevance=0.5, upvotes=5, downvotes=0, seen_count=5,
                           age_days=100, explore=0.0)
    down = config.usefulness(relevance=0.5, upvotes=0, downvotes=5, seen_count=5,
                            age_days=100, explore=0.0)
    assert up["score"] > down["score"]
    assert up["reputation"] > 0 > down["reputation"]


def test_usefulness_explore_lifts_unseen():
    from awm.precedence import config
    # With explore on, a never-seen entry gets a bigger bonus than a well-seen one.
    unseen = config.usefulness(relevance=0.5, upvotes=0, downvotes=0, seen_count=0,
                              age_days=100, explore=1.0)
    seen = config.usefulness(relevance=0.5, upvotes=0, downvotes=0, seen_count=50,
                            age_days=100, explore=1.0)
    assert unseen["explore"] > seen["explore"]
    # explore=0 zeroes the bonus entirely
    off = config.usefulness(relevance=0.5, upvotes=0, downvotes=0, seen_count=0,
                           age_days=100, explore=0.0)
    assert off["explore"] == 0.0


def test_recency_decays():
    from awm.precedence import config
    assert config.recency(0) == 1.0
    assert config.recency(config.RECENCY_HALFLIFE_DAYS) == pytest.approx(0.5, abs=1e-6)
    assert config.recency(None) == 0.0


# ---------------------------------------------------------------------------
# Non-embedding round trips: add-rejects, votes, notes, supersede, keyword
# ---------------------------------------------------------------------------


def test_add_requires_full_triple(conn):
    from awm.precedence import store
    with pytest.raises(ValueError):
        store.add(conn, context="", question="q", decision="d")


@needs_embed
def test_vote_and_note_reflected(conn):
    from awm.precedence import store
    d = store.add(conn, context="deploying a service to prod",
                  question="should I bump the version first?",
                  decision="always bump the patch version before deploying",
                  tag=["deploy"])
    did = d["id"]
    assert d["upvotes"] == 0 and d["notes"] == []

    store.vote(conn, did, direction="up")
    store.vote(conn, did, direction="up")
    store.vote(conn, did, direction="down")
    got = store.get(conn, did)
    assert got["upvotes"] == 2 and got["downvotes"] == 1

    store.note(conn, did, body="CI now auto-bumps the version", kind="context-change")
    got = store.get(conn, did)
    assert len(got["notes"]) == 1
    assert got["notes"][0]["kind"] == "context-change"


@needs_embed
def test_supersede_excluded_by_default(conn):
    from awm.precedence import store
    a = store.add(conn, context="choosing a python test runner",
                  question="pytest or unittest?", decision="use pytest")
    b = store.add(conn, context="choosing a python test runner",
                  question="pytest or nose2?", decision="use pytest with anyio")
    store.supersede(conn, a["id"], by=b["id"], note_body="refined preference")

    got = store.get(conn, a["id"])
    assert got["status"] == "superseded" and got["superseded_by"] == b["id"]

    # default search excludes superseded
    res = store.search(conn, keyword="pytest")
    ids = {r["id"] for r in res["results"]}
    assert a["id"] not in ids and b["id"] in ids
    # include_superseded re-admits it
    res2 = store.search(conn, keyword="pytest", include_superseded=True)
    assert a["id"] in {r["id"] for r in res2["results"]}


# ---------------------------------------------------------------------------
# Embedding-backed: fielded semantic search, reputation reorder, explore, impressions
# ---------------------------------------------------------------------------


@needs_embed
def test_fielded_semantic_search(conn):
    from awm.precedence import store
    a = store.add(conn,
                  context="an autonomous agent is unsure whether to open a pull request",
                  question="should the agent create the PR without asking?",
                  decision="agents may open draft PRs autonomously but not merge them")
    store.add(conn,
              context="picking a color for a chart in a dashboard",
              question="which palette should the chart use?",
              decision="use the colorblind-safe palette from the dataviz skill")

    # context-only query ranks the PR-autonomy entry first
    res = store.search(conn, context="the agent doesn't know if it can push code changes",
                       explore=0.0)
    assert res["results"][0]["id"] == a["id"]
    assert res["results"][0]["ranking"]["relevance"] > 0

    # adding a matching question field keeps it on top (both fields agree)
    res2 = store.search(conn,
                        context="the agent doesn't know if it can push code changes",
                        question="is the agent allowed to make the pull request itself?",
                        explore=0.0)
    assert res2["results"][0]["id"] == a["id"]


@needs_embed
def test_votes_reorder_equally_relevant(conn):
    from awm.precedence import store
    # Two near-identical-relevance entries; the upvoted one should rank higher.
    a = store.add(conn, context="handling a merge conflict during a rebase",
                  question="what to do on a conflict?", decision="abort and ask the user")
    b = store.add(conn, context="handling a merge conflict during a rebase",
                  question="what to do on a conflict?", decision="resolve it automatically")
    for _ in range(6):
        store.vote(conn, b["id"], direction="up")

    res = store.search(conn, context="merge conflict while rebasing", explore=0.0)
    order = [r["id"] for r in res["results"]]
    assert order.index(b["id"]) < order.index(a["id"])


@needs_embed
def test_impression_bumps_seen_count(conn):
    from awm.precedence import store
    d = store.add(conn, context="writing a commit message",
                  question="how long should the subject line be?",
                  decision="keep the subject under 72 chars")
    assert store.get(conn, d["id"])["seen_count"] == 0
    store.search(conn, keyword="commit", explore=0.0)
    assert store.get(conn, d["id"])["seen_count"] == 1
    store.search(conn, context="git commit subject length", explore=0.0)
    assert store.get(conn, d["id"])["seen_count"] == 2


@needs_embed
def test_import_manifest_idempotent(conn, tmp_path):
    from awm.precedence import store
    manifest = {
        "decisions": [
            {
                "context": "seeding from a memory file",
                "question": "should this be tagged as memory-sourced?",
                "decision": "yes, mark source=memory and keep the file ref",
                "source": "memory",
                "source_ref": "memory/feedback_example.md",
                "created": "2026-01-15",
                "tags": ["seeding", "provenance"],
                "notes": [{"body": "premise still holds", "kind": "comment"}],
            }
        ]
    }
    path = tmp_path / "staging.json"
    path.write_text(json.dumps(manifest))

    r1 = store.import_manifest(conn, manifest_path=str(path))
    assert r1["imported"] == 1 and r1["changed"] == 1
    # re-run: same stable id, no duplicate
    r2 = store.import_manifest(conn, manifest_path=str(path))
    assert r2["imported"] == 1 and r2["changed"] == 0
    assert store.stats(conn)["decisions"] == 1

    res = store.search(conn, context="bringing in a preference from a memory file")
    assert res["results"] and res["results"][0]["source"] == "memory"
    assert "seeding" in res["results"][0]["tags"]


@needs_embed
def test_merge_folds_dups(conn):
    from awm.precedence import store
    a = store.add(conn, context="naming a new service", question="underscore or hyphen?",
                  decision="single token, no underscore")
    b = store.add(conn, context="naming a new awm service", question="can it have an underscore?",
                  decision="no underscores in service names")
    store.vote(conn, b["id"], direction="up")
    store.note(conn, b["id"], body="see the naming memory", kind="comment")

    out = store.merge(conn, a["id"], [b["id"]])
    assert out["merged"] == 1
    assert store.get(conn, a["id"])["upvotes"] == 1
    assert len(store.get(conn, a["id"])["notes"]) == 1
    with pytest.raises(ValueError):
        store.get(conn, b["id"])  # gone


# ---------------------------------------------------------------------------
# Degrading honestly when the embedding stack is absent
#
# Precedence degrades loudest of the three services: semantic recall *is* its
# product, so a `count: 0` here would read as "no such precedent has ever been
# set". These run without the stack, which is what the `needs_embed` tests above
# can never observe.
# ---------------------------------------------------------------------------


def _unavailable(*a, **k):
    from awm.precedence.index import EmbeddingsUnavailable
    raise EmbeddingsUnavailable("sentence-transformers: not installed")


def _seed(conn, monkeypatch):
    from awm.precedence import index, store
    monkeypatch.setattr(index, "embed_decision", _unavailable)
    return store.add(conn, context="deploying a fix to the fleet",
                     question="merge to release or cherry-pick?",
                     decision="merge to release and push origin")


def test_add_lands_without_the_stack_and_leaves_the_stamp_stale(conn, monkeypatch):
    d = _seed(conn, monkeypatch)
    assert d["embedding_deferred"] is True
    row = conn.execute("SELECT content_hash, embedded_hash FROM decisions WHERE id=?",
                       (d["id"],)).fetchone()
    assert row["embedded_hash"] != row["content_hash"]


def test_search_degrades_to_listing_not_to_empty(conn, monkeypatch):
    from awm.precedence import index, store
    _seed(conn, monkeypatch)
    monkeypatch.setattr(index, "search_field", _unavailable)

    res = store.search(conn, context="rolling something out across machines")
    assert res["degraded"]["semantic"] == "unavailable"
    assert res["degraded"]["fallback"] == "listing"
    assert "precedence/install.sh" in res["degraded"]["fix"]
    assert res["count"] >= 1, "a missing dependency must not read as an empty archive"

    # Listing-mode hits were not shown because they were relevant, so they must
    # not accrue impressions — one node's outage would skew the whole archive's
    # explore/exploit term.
    assert conn.execute("SELECT MAX(seen_count) FROM decisions").fetchone()[0] == 0

    # A keyword leg still ranks, and says so.
    kw = store.search(conn, context="anything", keyword="release")
    assert kw["degraded"]["fallback"] == "keyword"
    assert kw["count"] >= 1


def test_embed_refuses_rather_than_reporting_zero(conn, monkeypatch):
    from awm.precedence import index, store

    monkeypatch.setattr(index, "probe",
                        lambda: {"available": False, "missing": ["sqlite_vec"]})
    with pytest.raises(index.EmbeddingsUnavailable):
        store.embed(conn)
