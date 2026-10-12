"""A foreign caller's grants ride the call to the service, and archive sources are
what a peer is granted.

The grant list reaches a service as ``args["_grants"]`` (see
``rpc.ControlChannel.call``): the adapter hands a handler only its ``args`` and
the caller stamp, so the arguments are the one place a handler can read it.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from awm.config import ARCHIVE_CATEGORY
from awm.gateway import catalog, peers
from awm.gateway.hub import discovery, rpc
from awm.gateway.hub.registry import ServiceRecord

pytestmark = [pytest.mark.unit]

FOREIGN = "peer:shaula"


class _Registry:
    def __init__(self, records):
        self._records = records

    def service_records(self):
        return list(self._records)


class _Channel:
    def __init__(self):
        self.seen = []

    async def call(self, fn, args, as_=None, timeout=None, grants=None):
        self.seen.append((fn, dict(args), grants))
        return {"ran": fn}


def _rec(name, *fns):
    return ServiceRecord(name=name, prefix=f"/svc/{name}", kind="service",
                         service_id=f"sid-{name}", api={"functions": [
                             {"name": n, "tool": t, "description": "d", "effect": e,
                              **({"category": c} if c else {})}
                             for n, t, e, c in fns]})


@pytest.fixture()
def world(awm_workspace, monkeypatch, tmp_path):
    stub = _Registry([
        _rec("search", ("query", "search_query", "read", ARCHIVE_CATEGORY),
             ("reindex", "search_reindex", "write", None),
             ("sweep", "search_sweep", "write", ARCHIVE_CATEGORY)),
        _rec("journal", ("fetch", "journal_fetch", "read", "journals")),
    ])
    monkeypatch.setattr(catalog, "get_registry", lambda: stub)
    ch = _Channel()
    monkeypatch.setattr(catalog.rpc, "get_control", lambda sid: ch)

    import awm.config.peerbook as peerbook

    book = tmp_path / "peers.json"
    book.write_text(json.dumps({
        "shaula": {"edge_url": "https://shaula:12100", "relation": "foreign",
                   "grants": ["precedence", "journals"]},
        "bare": {"edge_url": "https://bare:12100", "relation": "foreign", "grants": []},
        "mira": {"edge_url": "https://mira:12100", "relation": "domestic"},
    }))
    monkeypatch.setattr(peerbook, "peers_file", lambda: book)
    return ch


async def test_a_foreign_caller_reaches_an_archive_verb_with_its_grants(world):
    out = await catalog.dispatch("search_query", {"query": "x"}, as_=FOREIGN)
    assert json.loads(out) == {"ran": "query"}
    assert world.seen == [("query", {"query": "x"}, frozenset({"precedence", "journals"}))]


async def test_the_domain_shape_carries_the_grants_too(world):
    await catalog.dispatch("search", {"verb": "query", "args": {"query": "x"}}, as_=FOREIGN)
    assert world.seen[-1][2] == frozenset({"precedence", "journals"})


async def test_a_category_verb_receives_the_grants_as_well(world):
    await catalog.dispatch("journal_fetch", {}, as_=FOREIGN)
    assert world.seen[-1][2] == frozenset({"precedence", "journals"})


async def test_a_foreign_caller_with_no_grant_is_refused_an_archive_verb(world):
    with pytest.raises(ValueError, match="Unknown tool"):
        await catalog.dispatch("search_query", {"query": "x"}, as_="peer:bare")
    assert world.seen == []


async def test_the_archive_category_opens_only_a_read_verb(world):
    with pytest.raises(ValueError, match="Unknown tool"):
        await catalog.dispatch("search_sweep", {}, as_=FOREIGN)
    with pytest.raises(ValueError, match="Unknown tool"):
        await catalog.dispatch("search_reindex", {}, as_=FOREIGN)


async def test_a_domestic_caller_is_handed_no_grants(world):
    await catalog.dispatch("search_query", {"query": "x"}, as_="peer:mira")
    await catalog.dispatch("search_query", {"query": "x"})
    assert [g for _, _, g in world.seen] == [None, None]


def test_the_listing_a_foreign_caller_sees_holds_the_archive_verb():
    catalog_map = {"search": [
        {"verb": "query", "effect": "read", "category": ARCHIVE_CATEGORY},
        {"verb": "reindex", "effect": "write", "category": None}]}
    cut = catalog._foreign_catalog(catalog_map, frozenset({"journals"}))
    assert [v["verb"] for v in cut["search"]] == ["query"]
    assert catalog._foreign_catalog(catalog_map, frozenset()) == {}


# -- the channel envelope --------------------------------------------------------


async def _sent(args, grants):
    ch = rpc.ControlChannel("sid-env")
    task = asyncio.ensure_future(ch.call("fn", args, grants=grants, timeout=0.5))
    env = await ch.next_outbound()
    ch.handle_reply({"id": env["id"], "ok": True, "result": None})
    await task
    return env


async def test_the_channel_puts_the_sorted_grants_in_the_call_arguments():
    env = await _sent({"q": "x"}, frozenset({"kb", "journals"}))
    assert env["args"] == {"q": "x", "_grants": ["journals", "kb"]}


async def test_a_grants_argument_a_caller_supplied_never_survives():
    forged = {"q": "x", "_grants": ["everything"]}
    assert (await _sent(forged, None))["args"] == {"q": "x"}
    assert (await _sent(forged, frozenset({"kb"})))["args"]["_grants"] == ["kb"]
    assert forged["_grants"] == ["everything"], "the caller's dict is not mutated"


async def test_a_non_object_argument_passes_through():
    assert (await _sent(None, frozenset({"kb"})))["args"] is None


def test_notify_strips_a_supplied_grants_argument():
    ch = rpc.ControlChannel("sid-notify")
    ch.notify("fn", {"q": "x", "_grants": ["everything"]})
    assert ch._send_queue.get_nowait()["args"] == {"q": "x"}


# -- granting an archive source --------------------------------------------------


@pytest.fixture()
def book(awm_workspace, monkeypatch, tmp_path):
    import awm.config.peerbook as peerbook

    path = tmp_path / "peers.json"
    monkeypatch.setattr(peerbook, "peers_file", lambda: path)
    root = tmp_path / "services"
    for name, toml in {
            "precedence": '[[archive]]\nname = "precedence"\nmode = "index"\n',
            "notes": '[[archive]]\nname = "notes"\nmode = "index"\n'
                     '[[archive]]\nname = "writing"\nmode = "index"\n',
            "plain": 'tier = "on"\n'}.items():
        (root / name).mkdir(parents=True)
        (root / name / "run.sh").write_text("#!/bin/bash\n")
        (root / name / "service.toml").write_text(toml)
    monkeypatch.setenv("AWM_SERVICES_DIR", str(root))
    monkeypatch.setattr(catalog, "get_registry", lambda: _Registry([]))
    import awm.gateway.hub.registry as reg

    monkeypatch.setattr(reg, "get_registry", lambda: _Registry([]))
    peers.add("shaula", "shaula:12100", relation="foreign", swarm="other")
    return path


@pytest.mark.parametrize("source", ["precedence", "notes", "writing"])
def test_a_source_a_service_declares_can_be_granted(book, source):
    record, _ = peers.grant("shaula", source)
    assert source in record["grants"]


@pytest.mark.parametrize("legacy", ["journals", "kb"])
def test_the_grants_shaula_already_holds_stay_grantable(book, legacy):
    record, _ = peers.grant("shaula", legacy)
    assert legacy in record["grants"]


@pytest.mark.parametrize("bogus", ["bogus", "plain", ARCHIVE_CATEGORY, "Not A Slug"])
def test_anything_but_an_archive_source_is_refused(book, bogus):
    with pytest.raises(ValueError, match="archive source|slug"):
        peers.grant("shaula", bogus)


def test_a_stale_grant_can_still_be_revoked(book):
    record = peers.revoke("shaula", "retired-source")
    assert record["grants"] == []


def test_a_registered_services_archive_table_counts_too(book, monkeypatch):
    live = ServiceRecord(name="late", prefix="/svc/late", kind="service",
                         archive=[{"name": "late-source", "mode": "live"}])
    import awm.gateway.hub.registry as reg

    monkeypatch.setattr(reg, "get_registry", lambda: _Registry([live]))
    assert "late-source" in peers.grantable_sources()


# -- the archive tables in the service record ---------------------------------


def test_read_archive_returns_each_named_table(tmp_path):
    (tmp_path / "service.toml").write_text(
        'tier = "on"\n[[archive]]\nname = "a"\nmode = "index"\ntopic = "posts"\n'
        '[[archive]]\nmode = "live"\n[[archive]]\nname = "b"\nmode = "live"\n')
    assert discovery.read_archive(tmp_path) == [
        {"name": "a", "mode": "index", "topic": "posts"}, {"name": "b", "mode": "live"}]


def test_a_folder_with_no_archive_or_a_broken_file_has_none(tmp_path):
    assert discovery.read_archive(tmp_path) == []
    (tmp_path / "service.toml").write_text("[[archive]\nbroken")
    assert discovery.read_archive(tmp_path) == []


def test_the_archive_is_reread_when_the_file_changes(tmp_path):
    toml = tmp_path / "service.toml"
    toml.write_text('[[archive]]\nname = "a"\n')
    assert [t["name"] for t in discovery.read_archive(tmp_path)] == ["a"]
    toml.write_text('[[archive]]\nname = "a"\n[[archive]]\nname = "b"\n')
    assert [t["name"] for t in discovery.read_archive(tmp_path)] == ["a", "b"]


def test_the_ready_frame_fills_the_service_record_archive(tmp_path, monkeypatch):
    from awm.gateway.api import hub
    from awm.gateway.hub import registry

    root = tmp_path / "services"
    (root / "journal").mkdir(parents=True)
    (root / "journal" / "run.sh").write_text("#!/bin/bash\n")
    (root / "journal" / "service.toml").write_text(
        'tool = "journal"\n[[archive]]\nname = "journals"\nmode = "index"\ntopic = "posts"\n')
    monkeypatch.setenv("AWM_SERVICES_DIR", str(root))
    reg = registry.Registry()
    rec = ServiceRecord(name="journal", prefix="/svc/journal", kind="service")
    reg._install_base(rec)
    monkeypatch.setattr(hub, "get_registry", lambda: reg)
    ch = hub.rpc.ensure_control(rec.service_id)
    try:
        hub._route_inbound(ch, rec.service_id, {"kind": "ready", "api": {
            "functions": [{"name": "fetch", "tool": "journal_fetch"}]}})
        assert rec.archive == [{"name": "journals", "mode": "index", "topic": "posts"}]
    finally:
        hub.rpc.drop_control(rec.service_id)
