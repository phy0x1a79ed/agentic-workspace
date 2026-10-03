"""Pin resolution: what a scope needs, and what it cannot yet know."""

from __future__ import annotations

import json

import pytest

from awm.dvc import cache


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def put_object(cache_dir, h, body=""):
    obj = cache_dir / cache.object_relpath(h)
    obj.parent.mkdir(parents=True, exist_ok=True)
    obj.write_text(body)
    return obj


@pytest.fixture
def scope(tmp_path):
    s = tmp_path / "scope"
    (s / ".dvc").mkdir(parents=True)
    (s / ".dvc" / "cache").mkdir()
    return s


def test_object_relpath_shards_on_first_two_chars():
    assert cache.object_relpath("abcdef") == "files/md5/ab/cdef"


def test_object_relpath_keeps_dir_suffix_in_the_filename():
    # A .dir hash is stored like any other object; the suffix rides the filename,
    # so manifests and leaves share one addressing scheme.
    assert cache.object_relpath("ab1234.dir") == "files/md5/ab/1234.dir"


def test_find_pins_excludes_the_dvc_config_dir_but_not_pins_named_like_it(scope):
    write(scope / "data" / "swissprot.dvc", "outs:\n- md5: aaa\n")
    write(scope / ".dvc" / "tmp" / "stale.dvc", "outs:\n- md5: bbb\n")

    found = [p.name for p in cache.find_pins(scope)]

    # "swissprot.dvc" ends in ".dvc" too — a substring test would drop it.
    assert found == ["swissprot.dvc"]


def test_pin_hashes_reads_outs_and_ignores_deps(scope):
    pin = scope / "a.dvc"
    write(pin, "deps:\n- md5: dep000\n  path: x\nouts:\n- md5: out111\n  path: y\n")

    assert cache.pin_hashes(pin) == ["out111"]


def test_unparseable_pin_is_skipped_not_fatal(scope):
    pin = scope / "bad.dvc"
    write(pin, "outs:\n- md5: [unclosed\n")

    assert cache.pin_hashes(pin) == []


def test_resolve_expands_a_local_manifest_into_its_leaves(scope):
    cache_dir = scope / ".dvc" / "cache"
    manifest = "aa0000.dir"
    put_object(cache_dir, manifest, json.dumps([{"md5": "bb1111", "relpath": "f1"},
                                                {"md5": "cc2222", "relpath": "f2"}]))
    put_object(cache_dir, "bb1111", "present")
    write(scope / "d.dvc", f"outs:\n- md5: {manifest}\n")

    res = cache.resolve(scope, cache_dir)

    assert res.objects == {manifest, "bb1111", "cc2222"}
    assert res.unresolved == set()
    assert res.present == {manifest, "bb1111"}
    assert res.missing == {"cc2222"}


def test_absent_manifest_is_unresolved_and_hides_its_leaves(scope):
    # The two-phase problem: with the manifest cold, its leaves are not merely
    # missing — they are unnameable, and must not be counted as known.
    cache_dir = scope / ".dvc" / "cache"
    write(scope / "d.dvc", "outs:\n- md5: aa0000.dir\n")

    res = cache.resolve(scope, cache_dir)

    assert res.unresolved == {"aa0000.dir"}
    assert res.objects == {"aa0000.dir"}


def test_cache_dir_config_local_wins_over_config(scope, tmp_path):
    shared = tmp_path / "shared_cache"
    shared.mkdir()
    write(scope / ".dvc" / "config", '[cache]\n    dir = /wrong/path\n')
    write(scope / ".dvc" / "config.local", f'[cache]\n    dir = {shared}\n')

    assert cache.cache_dir_for(scope) == shared


def test_cache_dir_relative_is_anchored_to_the_dvc_dir(scope):
    write(scope / ".dvc" / "config", "[cache]\n    dir = ../shared\n")

    assert cache.cache_dir_for(scope) == (scope / "shared").resolve()


def test_cache_dir_defaults_when_unconfigured(scope):
    assert cache.cache_dir_for(scope) == scope / ".dvc" / "cache"


def test_open_scope_rejects_a_non_dvc_path(tmp_path):
    with pytest.raises(cache.DvcScopeError, match="not a DVC scope"):
        cache.open_scope(str(tmp_path))


def test_open_scope_rejects_a_cache_outside_the_workspace(scope, tmp_path, monkeypatch):
    # Chinook paths are addressed workspace-relative, so an outside cache has no
    # representable remote location — better to refuse than to push it somewhere wrong.
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    write(scope / ".dvc" / "config", f"[cache]\n    dir = {outside}\n")
    monkeypatch.setattr("awm.dvc.config.WORKSPACE_ROOT", scope)

    with pytest.raises(cache.DvcScopeError, match="outside"):
        cache.open_scope(str(scope))
