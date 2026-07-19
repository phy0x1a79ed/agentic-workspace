"""Unit tests for the in-memory hub Registry."""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.unit, pytest.mark.smoke]

import asyncio

import pytest

from awm.gateway.hub.registry import (
    NoBaseToShadow,
    PrefixConflict,
    Registry,
    ServiceRecord,
)


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


# ---------------------------------------------------------------------------
# Page registration (L1) — idempotent re-register keeps boot re-derive safe
# ---------------------------------------------------------------------------


def test_register_page_base(reg, tmp_path):
    rec = _run(reg.register_page("fleet", "/ui/fleet", str(tmp_path)))
    assert rec.kind == "page"
    assert rec.backend_status == "ready"          # static; nothing to await
    assert reg.longest_match("/ui/fleet") is rec
    assert reg.longest_match("/ui/fleet/anything") is rec


def test_register_page_idempotent_replaces_in_place(reg, tmp_path):
    # Re-registering the same page (a second gateway boot) replaces the base in
    # place rather than raising — what makes bootstrap_discovered_pages safe to
    # re-run on every restart.
    d1 = tmp_path / "a"; d1.mkdir()
    d2 = tmp_path / "b"; d2.mkdir()
    _run(reg.register_page("fleet", "/ui/fleet", str(d1)))
    rec2 = _run(reg.register_page("fleet", "/ui/fleet", str(d2)))
    assert reg.longest_match("/ui/fleet") is rec2
    assert rec2.static_dir == str(d2)


def test_register_page_conflict_different_name(reg, tmp_path):
    _run(reg.register_page("fleet", "/ui/fleet", str(tmp_path)))
    with pytest.raises(PrefixConflict):
        _run(reg.register_page("other", "/ui/fleet", str(tmp_path)))


def test_register_page_rejects_non_ui_prefix(reg, tmp_path):
    with pytest.raises(PrefixConflict):
        _run(reg.register_page("bad", "/svc/bad", str(tmp_path)))


def test_shadow_over_autoregistered_page_base(reg, tmp_path):
    # A page base auto-registered on boot must be shadowable — `awm dev shadow
    # pages/<name>` overlays a worktree build on top of the discovered base.
    base = _run(reg.register_page("fleet", "/ui/fleet", str(tmp_path / "base")))
    (tmp_path / "base").mkdir()
    overlay, evicted = _run(reg.replace_overlays(
        _overlay("shadow:fleet", "/ui/fleet")))
    assert evicted == []
    assert reg.longest_match("/ui/fleet") is overlay
    # Popping the overlay restores the discovered base.
    _run(reg.evict_by_id(overlay.service_id))
    assert reg.longest_match("/ui/fleet") is base


# ---------------------------------------------------------------------------
# Shadow overlay stack
# ---------------------------------------------------------------------------


def _overlay(name: str, prefix: str, url: str = "http://localhost:9") -> ServiceRecord:
    return ServiceRecord(name=name, prefix=prefix, kind="url", url=url)


def test_replace_overlays_requires_base(reg):
    with pytest.raises(NoBaseToShadow):
        _run(reg.replace_overlays(_overlay("ghost", "/x")))


def test_replace_overlays_returns_active_record(reg):
    base = _run(reg.register("base", "/x", "http://localhost:1"))
    overlay, evicted = _run(reg.replace_overlays(_overlay("ov", "/x", "http://localhost:2")))
    # longest_match returns the topmost — overlay shadows base; nothing evicted.
    assert evicted == []
    assert reg.longest_match("/x") is overlay
    assert reg.longest_match("/x/sub") is overlay
    assert overlay.is_overlay is True
    assert base.is_overlay is False


def test_replace_overlays_evicts_existing_overlays_keeps_base(reg):
    # Last connect wins: a second overlay evicts the first; base survives.
    base = _run(reg.register("base", "/x", "http://localhost:1"))
    ov1, ev1 = _run(reg.replace_overlays(_overlay("ov1", "/x", "http://localhost:2")))
    assert ev1 == []
    assert reg.longest_match("/x") is ov1
    ov2, ev2 = _run(reg.replace_overlays(_overlay("ov2", "/x", "http://localhost:3")))
    # ov1 was evicted and returned; ov2 is now the sole overlay.
    assert [r.service_id for r in ev2] == [ov1.service_id]
    assert reg.longest_match("/x") is ov2
    assert reg.get_by_name("url", "ov1") is None
    # Base survives; popping ov2's lease falls straight back to it.
    _run(reg.evict_by_id(ov2.service_id))
    assert reg.longest_match("/x") is base
    assert not reg.is_empty()


def test_replace_overlays_same_name_evicts_incumbent(reg):
    # Two worktrees both auto-name `ov` — the newcomer evicts the incumbent
    # instead of being rejected (the old PrefixConflict behavior is gone).
    _run(reg.register("base", "/x", "http://localhost:1"))
    ov_a, _ = _run(reg.replace_overlays(_overlay("ov", "/x", "http://localhost:2")))
    ov_b, evicted = _run(reg.replace_overlays(_overlay("ov", "/x", "http://localhost:3")))
    assert [r.service_id for r in evicted] == [ov_a.service_id]
    assert reg.longest_match("/x") is ov_b
    assert reg.get_by_id(ov_a.service_id) is None


def test_replace_overlays_rejects_base_name(reg):
    # The overlay may reuse another overlay's name, but NOT the base's —
    # `_by_name` is (kind, name)-keyed and the base owns that slot.
    _run(reg.register("base", "/x", "http://localhost:1"))
    with pytest.raises(PrefixConflict):
        _run(reg.replace_overlays(_overlay("base", "/x", "http://localhost:2")))


def test_evict_overlay_never_collapses_base(reg):
    base = _run(reg.register("base", "/x", "http://localhost:1"))
    _run(reg.replace_overlays(_overlay("ov", "/x", "http://localhost:2")))
    _run(reg.evict_by_name("ov"))
    # Base is still resolvable, both via longest_match and by_name.
    assert reg.longest_match("/x") is base
    assert reg.get_by_name("url", "base") is base
    # Overlay name is gone.
    assert reg.get_by_name("url", "ov") is None


def test_evict_base_with_overlay_present_leaves_overlay(reg):
    """_pop_by_id_locked pops one record at a time. Evicting the base while
    an overlay is still attached leaves the overlay as the sole stack entry
    (the `if not stack: del` branch only fires when the stack empties).
    The base's name is gone; the overlay continues to serve."""
    base = _run(reg.register("base", "/x", "http://localhost:1"))
    overlay, _ = _run(reg.replace_overlays(_overlay("ov", "/x")))
    _run(reg.evict_by_id(base.service_id))
    assert reg.get_by_name("url", "base") is None
    assert reg.longest_match("/x") is overlay


def test_evict_only_record_clears_prefix(reg):
    """When the last record on a prefix is evicted (no overlays), the
    stack entry is deleted and the prefix stops resolving."""
    base = _run(reg.register("base", "/x", "http://localhost:1"))
    _run(reg.evict_by_id(base.service_id))
    assert reg.longest_match("/x") is None
    assert reg.is_empty()


# ---------------------------------------------------------------------------
# Same-name page + service coexistence (kind-scoped uniqueness)
# ---------------------------------------------------------------------------


def test_same_name_page_and_service_coexist_page_first(reg, tmp_path):
    page = _run(reg.register_page("tts", "/ui/tts", str(tmp_path)))
    svc = _run(reg.register_service(
        "tts", "/svc/tts",
        pid=None, start_cmd=["start.sh"], cwd=str(tmp_path),
    ))
    assert reg.get_by_name("page", "tts") is page
    assert reg.get_by_name("service", "tts") is svc
    assert reg.longest_match("/ui/tts") is page
    assert reg.longest_match("/svc/tts") is svc


def test_same_name_page_and_service_coexist_service_first(reg, tmp_path):
    svc = _run(reg.register_service(
        "tts", "/svc/tts",
        pid=None, start_cmd=["start.sh"], cwd=str(tmp_path),
    ))
    page = _run(reg.register_page("tts", "/ui/tts", str(tmp_path)))
    assert reg.get_by_name("page", "tts") is page
    assert reg.get_by_name("service", "tts") is svc


def test_same_kind_same_name_distinct_prefix_still_rejected(reg):
    _run(reg.register("dup", "/a", "http://localhost:1"))
    with pytest.raises(PrefixConflict):
        _run(reg.register("dup", "/b", "http://localhost:2"))


def test_evict_by_name_ambiguous_without_kind_raises(reg, tmp_path):
    _run(reg.register_page("tts", "/ui/tts", str(tmp_path)))
    _run(reg.register_service(
        "tts", "/svc/tts",
        pid=None, start_cmd=["start.sh"], cwd=str(tmp_path),
    ))
    with pytest.raises(PrefixConflict):
        _run(reg.evict_by_name("tts"))
    # Disambiguating by kind succeeds and leaves the other in place.
    evicted = _run(reg.evict_by_name("tts", kind="page"))
    assert evicted is not None and evicted.kind == "page"
    assert reg.get_by_name("page", "tts") is None
    assert reg.get_by_name("service", "tts") is not None
