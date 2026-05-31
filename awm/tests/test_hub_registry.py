"""Unit tests for the in-memory hub Registry."""

from __future__ import annotations

import asyncio

import pytest

from awm.services.hub.registry import PrefixConflict, Registry


@pytest.fixture()
def reg():
    return Registry()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_register_and_match(reg):
    rec = _run(reg.register("foo", "/foo", "http://localhost:9001"))
    assert rec.prefix == "/foo"
    assert reg.longest_match("/foo") is rec
    assert reg.longest_match("/foo/anything") is rec
    assert reg.longest_match("/foobar") is None
    assert reg.longest_match("/other") is None


def test_longest_prefix_wins(reg):
    parent = _run(reg.register("p", "/api", "http://localhost:1"))
    child = _run(reg.register("c", "/api/v2", "http://localhost:2"))
    assert reg.longest_match("/api/health") is parent
    assert reg.longest_match("/api/v2/users") is child
    assert reg.longest_match("/api/v2") is child


def test_prefix_conflict(reg):
    _run(reg.register("a", "/dup", "http://localhost:1"))
    with pytest.raises(PrefixConflict):
        _run(reg.register("b", "/dup", "http://localhost:2"))


def test_evict_by_id_clears_lookup(reg):
    rec = _run(reg.register("foo", "/foo", "http://localhost:9001"))
    assert reg.longest_match("/foo") is rec
    evicted = _run(reg.evict_by_id(rec.service_id))
    assert evicted is rec
    assert reg.longest_match("/foo") is None
    assert reg.is_empty()


def test_evict_by_name_returns_record(reg):
    _run(reg.register("x", "/x", "http://localhost:1"))
    evicted = _run(reg.evict_by_name("x"))
    assert evicted is not None and evicted.name == "x"
    assert _run(reg.evict_by_name("x")) is None


def test_normalization(reg):
    rec = _run(reg.register("foo", "foo/", "http://localhost:9001/"))
    assert rec.prefix == "/foo"
    assert rec.url == "http://localhost:9001"


def test_is_empty_fast_path(reg):
    assert reg.is_empty()
    assert reg.longest_match("/anything") is None
    _run(reg.register("a", "/a", "http://x"))
    assert not reg.is_empty()


def test_re_registering_same_name_updates(reg):
    # Same name, same prefix → no conflict (idempotent re-register).
    rec1 = _run(reg.register("foo", "/foo", "http://localhost:1"))
    rec2 = _run(reg.register("foo", "/foo", "http://localhost:2"))
    assert rec1.name == rec2.name == "foo"
    assert rec2.url == "http://localhost:2"
