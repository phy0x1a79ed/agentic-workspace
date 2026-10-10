"""Federation surface of scopes: edge-stamped origins, verb effects, archive search."""

from __future__ import annotations

import asyncio
import json

import pytest

pytestmark = [pytest.mark.scopes]


# ---------------------------------------------------------------------------
# Origin comes from the edge identity, never from the caller's `author`
# ---------------------------------------------------------------------------

def _post(as_, author="user:mallory", body="hello", kind="message"):
    from awm.scopes.operations.scope_channel import _handle_scope_post
    args = {"project": "awm", "scope": "dev", "author": author, "body": body, "kind": kind}
    return _handle_scope_post(args, as_)["post"]


@pytest.mark.parametrize("as_", ["peer:capella", "peer:shaula", "peer"])
def test_peer_identity_replaces_the_callers_author(scopes_workspace, as_):
    post = _post(as_, author="agent:awm/dev")
    assert post["author"] == as_


def test_peer_origin_applies_to_journals_and_messages(scopes_workspace):
    assert _post("peer:capella", kind="journal", body="debrief")["author"] == "peer:capella"
    assert _post("peer:capella", kind="message", body="ping")["author"] == "peer:capella"


def test_peer_origin_survives_a_refetch_and_an_author_filter(scopes_workspace):
    from awm.scopes import channel
    post = _post("peer:capella")
    assert channel.get_post(post["id"]).author == "peer:capella"
    found = channel.fetch(project="awm", scope="dev", author="peer:capella")
    assert [p.id for p in found] == [post["id"]]


@pytest.mark.parametrize("as_", [None, "tony", "agent:awm/dev", "user:tony"])
def test_other_callers_keep_the_author_they_sent(scopes_workspace, as_):
    assert _post(as_, author="agent:awm/dev")["author"] == "agent:awm/dev"


def test_a_local_caller_cannot_claim_a_peer_author(scopes_workspace):
    """Only the edge stamp can mint `peer:`; an unstamped caller sending one is refused."""
    from awm.scopes import channel
    with pytest.raises(ValueError, match="reserved"):
        _post(None, author="peer:capella")
    with pytest.raises(ValueError, match="reserved"):
        _post("tony", author="peer:capella")
    assert channel.fetch(project="awm", scope="dev") == []


def test_the_senders_claimed_author_is_kept_in_meta(scopes_workspace):
    stamped = _post("peer:capella", author="agent:awm/dev")
    assert stamped["author"] == "peer:capella"
    assert stamped["meta"]["claimed_author"] == "agent:awm/dev"
    assert "claimed_author" not in _post(None, author="agent:awm/dev")["meta"]


def test_claimed_author_does_not_mutate_the_callers_meta(scopes_workspace):
    from awm.scopes.operations.scope_channel import _handle_scope_post
    meta = {"title": "t"}
    _handle_scope_post({"project": "awm", "scope": "dev", "author": "agent:awm/dev", "body": "x",
                        "kind": "journal", "meta": meta}, "peer:capella")
    assert meta == {"title": "t"}


# ---------------------------------------------------------------------------
# A foreign caller reads journals only
# ---------------------------------------------------------------------------

@pytest.fixture()
def peer_book(monkeypatch):
    import awm.config as cfg
    records = {
        "capella": {"name": "capella", "relation": "foreign", "grants": ["journals"]},
        "orion": {"name": "orion", "relation": "domestic", "grants": []},
    }
    monkeypatch.setattr(cfg, "peer_record", lambda n: dict(records[n]) if n in records else None)
    return records


def _fetch(as_, **args):
    from awm.scopes.operations.scope_channel import _handle_scope_fetch
    return _handle_scope_fetch({"project": "awm", "scope": "dev", **args}, as_)


@pytest.mark.parametrize("as_,foreign", [
    ("peer:capella", True), ("peer:stranger", True), ("peer:", True), ("peer: ", True),
    ("peer:orion", False), ("peer", False), (None, False), ("tony", False),
])
def test_is_foreign_matches_the_gateway_gate(peer_book, as_, foreign):
    from awm.scopes import channel
    assert channel.is_foreign(as_) is foreign


def test_a_foreign_fetch_returns_journals_only(scopes_workspace, peer_book):
    _post(None, author="agent:awm/dev", body="private chatter", kind="message")
    _post(None, author="agent:awm/dev", body="the debrief", kind="journal")
    assert [p["kind"] for p in _fetch("peer:capella")["posts"]] == ["journal"]
    assert [p["kind"] for p in _fetch("peer:capella", kind="journal")["posts"]] == ["journal"]
    assert len(_fetch("peer:orion")["posts"]) == 2
    assert len(_fetch(None)["posts"]) == 2


@pytest.mark.parametrize("kind", ["message", "goal", "system"])
def test_a_foreign_fetch_of_another_kind_is_refused(scopes_workspace, peer_book, kind):
    with pytest.raises(PermissionError):
        _fetch("peer:capella", kind=kind)
    assert _fetch("peer:orion", kind=kind) is not None


def test_a_foreign_fetch_by_post_id_needs_a_journal(scopes_workspace, peer_book):
    msg = _post(None, author="agent:awm/dev", body="chatter", kind="message")
    jr = _post(None, author="agent:awm/dev", body="debrief", kind="journal")
    with pytest.raises(PermissionError):
        _fetch("peer:capella", post_id=msg["id"])
    assert _fetch("peer:capella", post_id=jr["id"])["total"] == 1
    assert _fetch("peer:orion", post_id=msg["id"])["total"] == 1
    assert _fetch("peer:capella", post_id="nope")["total"] == 0


def test_a_foreign_search_sees_journals_only(scopes_workspace, peer_book):
    from awm.scopes import search_index
    _post(None, author="agent:awm/dev", body="mooring mast chatter", kind="message")
    _post(None, author="agent:awm/dev", body="mooring mast debrief", kind="journal")
    search_index.flush()
    posts = _fetch("peer:capella", query="mooring mast")["posts"]
    assert [p["kind"] for p in posts] == ["journal"]


def test_goal_reads_are_refused_to_foreign_callers_only(scopes_workspace, peer_book):
    from awm.scopes.operations.goals import _handle_goal_history, _handle_goal_read, _handle_goal_set
    goal = _handle_goal_set({"objective": "secret aim", "author": "user:tony", "level": "scope",
                             "project": "awm", "scope": "dev"})["goal"]
    with pytest.raises(PermissionError):
        _handle_goal_read({"project": "awm", "scope": "dev"}, "peer:capella")
    with pytest.raises(PermissionError):
        _handle_goal_history({"goal_id": goal["id"]}, "peer:stranger")
    assert _handle_goal_read({"project": "awm", "scope": "dev"}, "peer:orion")["total"] == 1
    assert _handle_goal_read({"project": "awm", "scope": "dev"}, None)["total"] == 1
    assert _handle_goal_history({"goal_id": goal["id"]})["total"] == 1


def test_goal_writes_carry_the_peer_origin(scopes_workspace):
    from awm.scopes.operations.goals import _handle_goal_retire, _handle_goal_set
    made = _handle_goal_set({"objective": "ship it", "author": "user:mallory", "level": "scope",
                             "project": "awm", "scope": "dev"}, "peer:capella")["goal"]
    assert made["author"] == "peer:capella"
    local = _handle_goal_set({"objective": "own goal", "author": "user:tony", "level": "scope",
                              "project": "awm", "scope": "dev"})["goal"]
    assert local["author"] == "user:tony"
    tomb = _handle_goal_retire({"goal_id": made["id"], "author": "user:mallory"},
                               "peer:shaula")["tombstone"]
    assert tomb["author"] == "peer:shaula"


# ---------------------------------------------------------------------------
# Every verb declares an effect; journal-bearing reads declare the category
# ---------------------------------------------------------------------------

def _functions():
    from awm.scopes.hub_adapter import API_MANIFEST
    return {f["name"]: f for f in API_MANIFEST["functions"]}


def test_every_scopes_verb_declares_an_effect():
    from awm.config import EFFECTS
    missing = [n for n, f in _functions().items() if f.get("effect") not in EFFECTS]
    assert missing == []


READS = {"scope_search", "scope_data_status", "project_search", "resolveScope", "resolveRef",
         "scope_fetch", "scope_archive_search", "scope_goal_read", "scope_goal_history"}
JOURNAL_READS = {"scope_fetch", "scope_archive_search", "scope_goal_read", "scope_goal_history"}


def test_only_the_known_reads_are_reads():
    reads = {n for n, f in _functions().items() if f["effect"] == "read"}
    assert reads == READS


def test_journal_text_reads_carry_the_journals_category_and_nothing_else_does():
    categorised = {n: f["category"] for n, f in _functions().items() if f.get("category")}
    assert categorised == {n: "journals" for n in JOURNAL_READS}


def test_the_archive_verb_has_a_handler_and_a_scope_domain_tool():
    from awm.scopes.hub_adapter import HANDLERS
    fn = _functions()["scope_archive_search"]
    assert fn["tool"].startswith("scope_")
    assert "scope_archive_search" in HANDLERS


# ---------------------------------------------------------------------------
# Archive search
# ---------------------------------------------------------------------------

def _journal(project, scope, body):
    from awm.scopes import channel
    return channel.post(project, scope, author=f"agent:{project}/{scope}", kind="journal", body=body)


def _remote_post(pid, body, ts, **extra):
    return {"id": pid, "project": "p", "scope": "s", "author": "agent:p/s", "kind": "journal",
            "body": body, "meta": {}, "ts": ts, **extra}


@pytest.fixture()
def book(monkeypatch):
    """Two foreign-or-domestic peers in the book and a named local node and swarm."""
    import awm.config as cfg
    records = [
        {"name": "capella", "swarm": "mock", "relation": "foreign", "grants": ["journals"]},
        {"name": "shaula", "swarm": "collins", "relation": "foreign", "grants": []},
    ]
    monkeypatch.setattr(cfg, "list_records", lambda: [dict(r) for r in records])
    monkeypatch.setenv("AWM_SWARM", "tony")
    monkeypatch.setenv("AWM_NODE_NAME", "altair")
    return records


@pytest.fixture()
def fake_peers(monkeypatch):
    """Replace the peer transport. `replies[peer]` is a reply, or an exception to raise."""
    from awm.scopes import archive
    state = {"calls": [], "replies": {}}

    async def fake(peer, name, args, *, timeout):
        state["calls"].append((peer, name, dict(args), timeout))
        reply = state["replies"][peer]
        if callable(reply):
            return await reply()
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr(archive, "_invoke_peer", fake)
    return state


async def test_hits_merge_with_origin_tags(scopes_workspace, book, fake_peers):
    from awm.scopes import search_index
    from awm.scopes.archive import archive_search
    _journal("awm", "dev", "the zeppelin mooring mast leaked hydraulic fluid")
    search_index.flush()
    fake_peers["replies"]["capella"] = {"posts": [
        _remote_post("c1", "capella mooring mast notes", "2026-10-01T00:00:00Z",
                     origin_swarm="forged", origin_node="forged")]}
    fake_peers["replies"]["shaula"] = {"posts": [_remote_post("s1", "shaula mast", "2026-10-02T00:00:00Z")]}

    out = await archive_search({"query": "mooring mast"})

    assert out["peers"] == {"capella": "ok", "shaula": "ok"}
    by_id = {h["id"]: h for h in out["hits"]}
    assert len(out["hits"]) == 3
    local = next(h for h in out["hits"] if "zeppelin" in h["body"])
    assert (local["origin_swarm"], local["origin_node"]) == ("tony", "altair")
    assert (by_id["c1"]["origin_swarm"], by_id["c1"]["origin_node"]) == ("mock", "capella")
    assert (by_id["s1"]["origin_swarm"], by_id["s1"]["origin_node"]) == ("collins", "shaula")


async def test_every_peer_is_asked_the_local_search_verb_never_the_archive(
        scopes_workspace, book, fake_peers):
    from awm.scopes.archive import archive_search
    for name in ("capella", "shaula"):
        fake_peers["replies"][name] = {"posts": []}
    await archive_search({"query": "mast", "project": "awm", "scope": "dev", "limit": 7})

    assert sorted(c[0] for c in fake_peers["calls"]) == ["capella", "shaula"]
    for _peer, verb, args, timeout in fake_peers["calls"]:
        assert verb == "scope_fetch"
        assert args == {"query": "mast", "kind": "journal", "limit": 7, "project": "awm", "scope": "dev"}
        assert timeout == pytest.approx(10.0)


async def test_a_peer_that_refuses_is_reported_and_the_rest_still_answer(
        scopes_workspace, book, fake_peers):
    from awm.scopes.archive import archive_search
    fake_peers["replies"]["capella"] = {"posts": [_remote_post("c1", "mast", "2026-10-01T00:00:00Z")]}
    fake_peers["replies"]["shaula"] = PermissionError("403: journals not granted")

    out = await archive_search({"query": "mast"})

    assert out["peers"]["capella"] == "ok"
    assert "journals not granted" in out["peers"]["shaula"]["error"]
    assert [h["id"] for h in out["hits"]] == ["c1"]


async def test_an_error_body_counts_as_a_failure(scopes_workspace, book, fake_peers):
    from awm.scopes.archive import archive_search
    fake_peers["replies"]["capella"] = {"error": "verb refused: category not granted"}
    fake_peers["replies"]["shaula"] = {"posts": []}
    out = await archive_search({"query": "mast"})
    assert "not granted" in out["peers"]["capella"]["error"]
    assert out["peers"]["shaula"] == "ok"


async def test_a_hung_peer_times_out_without_failing_the_search(
        scopes_workspace, book, fake_peers, monkeypatch):
    from awm.scopes import archive
    monkeypatch.setattr(archive, "PEER_TIMEOUT_S", 0.05)

    async def hang():
        await asyncio.sleep(30)

    fake_peers["replies"]["capella"] = hang
    fake_peers["replies"]["shaula"] = {"posts": [_remote_post("s1", "mast", "2026-10-02T00:00:00Z")]}

    out = await asyncio.wait_for(archive.archive_search({"query": "mast"}), timeout=5)

    assert "timeout" in out["peers"]["capella"]["error"]
    assert out["peers"]["shaula"] == "ok"
    assert [h["id"] for h in out["hits"]] == ["s1"]


async def test_transport_wrapped_replies_are_unwrapped(scopes_workspace, book, fake_peers):
    from awm.scopes.archive import archive_search
    posts = {"posts": [_remote_post("c1", "mast", "2026-10-01T00:00:00Z")]}
    fake_peers["replies"]["capella"] = {"result": json.dumps(posts)}
    fake_peers["replies"]["shaula"] = json.dumps(posts)
    out = await archive_search({"query": "mast"})
    assert out["peers"] == {"capella": "ok", "shaula": "ok"}
    assert len(out["hits"]) == 2


async def test_peers_argument_limits_the_fan_out_and_names_strangers(
        scopes_workspace, book, fake_peers):
    from awm.scopes.archive import archive_search
    fake_peers["replies"]["capella"] = {"posts": []}
    out = await archive_search({"query": "mast", "peers": ["capella", "nowhere"]})
    assert [c[0] for c in fake_peers["calls"]] == ["capella"]
    assert out["peers"]["capella"] == "ok"
    assert "peer book" in out["peers"]["nowhere"]["error"]


async def test_a_call_that_arrives_from_a_peer_searches_this_node_only(
        scopes_workspace, book, fake_peers):
    from awm.scopes import search_index
    from awm.scopes.archive import archive_search
    _journal("awm", "dev", "the zeppelin mooring mast")
    search_index.flush()
    for as_ in ("peer:capella", "peer"):
        out = await archive_search({"query": "mooring mast"}, as_)
        assert out["peers"] == {}
        assert [h["origin_node"] for h in out["hits"]] == ["altair"]
    assert fake_peers["calls"] == []


async def test_limit_applies_to_the_merged_list(scopes_workspace, book, fake_peers):
    from awm.scopes.archive import archive_search
    fake_peers["replies"]["capella"] = {"posts": [
        _remote_post(f"c{i}", "mast", f"2026-10-0{i + 1}T00:00:00Z") for i in range(5)]}
    fake_peers["replies"]["shaula"] = {"posts": []}
    out = await archive_search({"query": "mast", "limit": 3})
    assert len(out["hits"]) == 3


async def test_an_empty_book_is_a_local_search(scopes_workspace, monkeypatch, fake_peers):
    import awm.config as cfg
    from awm.scopes import search_index
    from awm.scopes.archive import archive_search
    monkeypatch.setattr(cfg, "list_records", lambda: [])
    _journal("awm", "dev", "the zeppelin mooring mast")
    search_index.flush()
    out = await archive_search({"query": "mooring mast"})
    assert out["peers"] == {}
    assert len(out["hits"]) == 1


async def test_a_blank_query_is_refused(scopes_workspace, book, fake_peers):
    from awm.scopes.archive import archive_search
    with pytest.raises(ValueError):
        await archive_search({"query": "  "})


async def test_this_node_is_never_asked_but_stations_are(scopes_workspace, book, fake_peers, monkeypatch):
    import awm.config as cfg
    from awm.scopes.archive import archive_search
    monkeypatch.setattr(cfg, "list_records", lambda: [
        {"name": "altair", "swarm": "tony", "relation": "domestic", "role": "fleet"},
        {"name": "Altair.local", "swarm": "tony", "relation": "domestic", "role": "fleet"},
        {"name": "deneb", "swarm": "tony", "relation": "domestic", "role": "station"},
    ])
    fake_peers["replies"]["deneb"] = {"posts": []}
    out = await archive_search({"query": "mast"})
    assert [c[0] for c in fake_peers["calls"]] == ["deneb"]
    assert out["peers"] == {"deneb": "ok"}


async def test_a_foreign_peer_sharing_our_swarm_is_tagged_unknown(scopes_workspace, monkeypatch, fake_peers):
    import awm.config as cfg
    from awm.scopes.archive import archive_search
    monkeypatch.setenv("AWM_SWARM", "tony")
    monkeypatch.setenv("AWM_NODE_NAME", "altair")
    monkeypatch.setattr(cfg, "list_records", lambda: [
        {"name": "capella", "swarm": "tony", "relation": "foreign"},
        {"name": "orion", "swarm": "tony", "relation": "domestic"},
    ])
    for name, pid in (("capella", "c1"), ("orion", "o1")):
        fake_peers["replies"][name] = {"posts": [_remote_post(pid, "mast", "2026-10-01T00:00:00Z")]}
    hits = {h["id"]: h for h in (await archive_search({"query": "mast"}))["hits"]}
    assert hits["c1"]["origin_swarm"] == "?"
    assert hits["o1"]["origin_swarm"] == "tony"


async def test_remote_hits_that_are_not_journals_are_dropped(scopes_workspace, book, fake_peers):
    from awm.scopes.archive import archive_search
    fake_peers["replies"]["capella"] = {"posts": [
        _remote_post("c1", "mast", "2026-10-01T00:00:00Z"),
        {**_remote_post("c2", "chat", "2026-10-02T00:00:00Z"), "kind": "message"},
        {**_remote_post("c3", "aim", "2026-10-03T00:00:00Z"), "kind": "goal"}]}
    fake_peers["replies"]["shaula"] = {"posts": []}
    out = await archive_search({"query": "mast"})
    assert [h["id"] for h in out["hits"]] == ["c1"]


async def test_one_peer_cannot_flood_the_result(scopes_workspace, book, fake_peers):
    from awm.scopes.archive import archive_search
    fake_peers["replies"]["capella"] = {"posts": [
        _remote_post(f"c{i}", "mast", "2026-10-01T00:00:00Z") for i in range(50)]}
    fake_peers["replies"]["shaula"] = {"posts": []}
    out = await archive_search({"query": "mast", "limit": 4})
    assert len(out["hits"]) == 4


async def test_an_oversized_reply_is_discarded(scopes_workspace, book, fake_peers, monkeypatch):
    from awm.scopes import archive
    monkeypatch.setattr(archive, "MAX_REPLY_BYTES", 500)
    fake_peers["replies"]["capella"] = {"posts": [_remote_post("c1", "x" * 2000, "2026-10-01T00:00:00Z")]}
    fake_peers["replies"]["shaula"] = {"posts": [_remote_post("s1", "mast", "2026-10-02T00:00:00Z")]}
    out = await archive.archive_search({"query": "mast"})
    assert "cap" in out["peers"]["capella"]["error"]
    assert [h["id"] for h in out["hits"]] == ["s1"]


def test_merge_interleaves_by_rank_and_breaks_ties_by_recency():
    from awm.scopes.archive import _merge
    local = [{"id": "l0", "ts": "2026-10-01"}, {"id": "l1", "ts": "2026-10-09"}]
    peer = [{"id": "p0", "ts": "2026-10-05"}, {"id": "p1", "ts": "2026-10-02"}]
    assert [h["id"] for h in _merge([local, peer], 10)] == ["p0", "l0", "l1", "p1"]
    assert [h["id"] for h in _merge([local, peer], 3)] == ["p0", "l0", "l1"]
