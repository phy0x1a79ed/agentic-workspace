"""Tree partitioning: cover everything except the checkouts, cache, and junk.

The destination-containment cases are the ones that matter most: this is the
only transfer in the service that deletes, and the thing it must never be able
to reach is the append-only cache archive sitting beside it on the collection.
"""

from __future__ import annotations

from awm.dvc import backup
from awm.dvc.config import ChinookConfig


def touch(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def sources(items):
    return {i["source_path"] for i in items}


def cfg_for(prefix="/backups/node"):
    return ChinookConfig(
        local_endpoint="local-uuid",
        remote_endpoint="remote-uuid",
        prefix=prefix,
        globus_bin="/nonexistent/globus",
    )


# --- partitioning -----------------------------------------------------------


def test_a_clean_tree_is_emitted_as_whole_recursive_dirs(tmp_path):
    touch(tmp_path / "a" / "deep" / "f")
    touch(tmp_path / "b" / "g")

    items = backup.partition(str(tmp_path), [], "/dest")

    # Nothing excluded beneath them, so each top-level dir goes as one item
    # rather than being walked into.
    assert sources(items) == {f"{tmp_path}/a/", f"{tmp_path}/b/"}
    assert all(i["recursive"] for i in items)


def test_an_excluded_path_is_dropped_and_its_siblings_survive(tmp_path):
    touch(tmp_path / "proj" / "assembly" / "out")
    touch(tmp_path / "proj" / "keep" / "src")

    items = backup.partition(str(tmp_path), [str(tmp_path / "proj" / "assembly")], "/dest")

    assert sources(items) == {f"{tmp_path}/proj/keep/"}


def test_a_name_shared_with_an_excluded_dir_elsewhere_is_not_dropped(tmp_path):
    # The reason filter_rules cannot be used: they match an item's *name* at any
    # depth, so excluding the DVC output "assembly" would also delete the
    # unrelated source directory that happens to share the name.
    touch(tmp_path / "data" / "assembly" / "out.bam")
    touch(tmp_path / "src" / "transforms" / "assembly" / "run.py")

    items = backup.partition(str(tmp_path), [str(tmp_path / "data" / "assembly")], "/dest")

    assert f"{tmp_path}/src/" in sources(items)
    assert not any("data/assembly" in s for s in sources(items))


def test_destination_paths_are_rebased_under_the_prefix(tmp_path):
    touch(tmp_path / "keep" / "f")
    touch(tmp_path / "drop" / "f")

    items = backup.partition(str(tmp_path), [str(tmp_path / "drop")], "/backups/node")

    assert [i["destination_path"] for i in items] == ["/backups/node/keep/"]


def test_a_lone_file_beside_an_exclusion_is_emitted_non_recursively(tmp_path):
    touch(tmp_path / "top.txt")
    touch(tmp_path / "drop" / "f")

    items = backup.partition(str(tmp_path), [str(tmp_path / "drop")], "/dest")

    assert items == [
        {
            "DATA_TYPE": "transfer_item",
            "source_path": f"{tmp_path}/top.txt",
            "destination_path": "/dest/top.txt",
            "recursive": False,
        }
    ]


def test_excluding_a_deep_path_still_covers_everything_above_it(tmp_path):
    touch(tmp_path / "a" / "b" / "c" / "excluded" / "f")
    touch(tmp_path / "a" / "b" / "sibling" / "f")
    touch(tmp_path / "a" / "other" / "f")

    items = backup.partition(
        str(tmp_path), [str(tmp_path / "a" / "b" / "c" / "excluded")], "/dest"
    )

    assert sources(items) == {
        f"{tmp_path}/a/b/sibling/",
        f"{tmp_path}/a/other/",
    }


# --- pin discovery ----------------------------------------------------------


def test_dvc_output_paths_collects_every_out_not_just_the_first(tmp_path):
    touch(
        tmp_path / "proj" / "multi.dvc",
        "outs:\n- md5: aaa\n  path: first\n- md5: bbb\n  path: second\n",
    )

    assert backup.dvc_output_paths(tmp_path) == [
        str(tmp_path / "proj" / "first"),
        str(tmp_path / "proj" / "second"),
    ]


def test_dvc_output_paths_resolves_relative_to_the_pin_not_the_root(tmp_path):
    touch(tmp_path / "deep" / "nest" / "d.dvc", "outs:\n- md5: aaa\n  path: out\n")

    assert backup.dvc_output_paths(tmp_path) == [str(tmp_path / "deep" / "nest" / "out")]


def test_an_unparseable_pin_does_not_abort_discovery(tmp_path):
    touch(tmp_path / "bad.dvc", "outs:\n- md5: [unclosed\n")
    touch(tmp_path / "good.dvc", "outs:\n- md5: aaa\n  path: out\n")

    assert backup.dvc_output_paths(tmp_path) == [str(tmp_path / "out")]


# --- regenerable directories ------------------------------------------------


def test_regenerable_dirs_are_found_by_name_at_any_depth(tmp_path):
    touch(tmp_path / "a" / "__pycache__" / "m.pyc")
    touch(tmp_path / "b" / "deep" / "node_modules" / "pkg" / "index.js")
    touch(tmp_path / "b" / "deep" / "src.py")

    assert backup.regenerable_paths(tmp_path) == sorted(
        [
            str(tmp_path / "a" / "__pycache__"),
            str(tmp_path / "b" / "deep" / "node_modules"),
        ]
    )


def test_regenerable_matching_never_prunes_a_file_that_shares_the_name(tmp_path):
    touch(tmp_path / "node_modules")  # a file, not a directory

    assert backup.regenerable_paths(tmp_path) == []


def test_regenerable_walk_does_not_descend_into_skipped_paths(tmp_path):
    # The cache holds 300 GB of content-addressed objects and no source; walking
    # it to find nothing is the whole cost this skip exists to avoid.
    cache = tmp_path / "data" / ".dvc_cache"
    touch(cache / "files" / "md5" / "ab" / "__pycache__" / "x")
    touch(tmp_path / "src" / "__pycache__" / "y")

    found = backup.regenerable_paths(tmp_path, skip=[str(cache)])

    assert found == [str(tmp_path / "src" / "__pycache__")]


# --- the assembled document -------------------------------------------------


def _workspace(tmp_path, monkeypatch):
    """A miniature workspace with a cache, a pinned output, and junk."""
    cache = tmp_path / "data" / ".dvc_cache"
    touch(cache / "files" / "md5" / "ab" / "cdef")
    touch(tmp_path / "projects" / "p" / "out.dvc", "outs:\n- md5: aaa\n  path: out\n")
    touch(tmp_path / "projects" / "p" / "out" / "big.bam")
    touch(tmp_path / "projects" / "p" / "__pycache__" / "m.pyc")
    touch(tmp_path / "projects" / "p" / "src.py")
    touch(tmp_path / ".awm" / "state.json")

    monkeypatch.setattr(backup, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(backup, "SHARED_CACHE", cache)
    return tmp_path


def test_the_document_excludes_the_cache_the_checkouts_and_the_junk(tmp_path, monkeypatch):
    ws = _workspace(tmp_path, monkeypatch)

    items, counts = backup.build_items(cfg_for())
    srcs = sources(items)

    assert not any(".dvc_cache" in s for s in srcs)
    assert not any(s.rstrip("/").endswith("/projects/p/out") for s in srcs)
    assert not any("__pycache__" in s for s in srcs)
    # ...while everything else is still covered.
    assert f"{ws}/projects/p/src.py" in srcs
    assert f"{ws}/projects/p/out.dvc" in srcs
    assert f"{ws}/.awm/" in srcs
    assert counts == {
        "dvc_outputs": 1,
        "shared_cache": 1,
        "regenerable_dirs": 1,
        "total": 3,
    }


def test_no_destination_escapes_the_workspace_root(tmp_path, monkeypatch):
    """The containment invariant: this transfer deletes, and the archive is a
    sibling of where it writes. No item may address a path outside it."""
    _workspace(tmp_path, monkeypatch)

    items, _ = backup.build_items(cfg_for("/backups/node"))

    assert items
    for item in items:
        assert item["destination_path"].startswith("/backups/node/workspace/")
    # Specifically: nothing can reach the append-only archive beside it.
    assert not any(
        i["destination_path"].startswith("/backups/node/data/") for i in items
    )


def test_the_destination_root_is_a_sibling_of_the_cache_archive_not_its_parent():
    cfg = cfg_for("/backups/node")

    assert backup.destination_root(cfg) == "/backups/node/workspace"
    # The archive writes to <prefix>/data/.dvc_cache/ — outside the mirror root.
    assert not "/backups/node/data/.dvc_cache".startswith(
        backup.destination_root(cfg) + "/"
    )


def test_the_mirror_submits_with_delete_enabled_and_mtime_comparison(monkeypatch):
    """Mirror semantics, asserted rather than merely commented."""
    seen = {}

    def fake_submit(cfg, items, **kwargs):
        seen.update(kwargs)
        seen["items"] = items
        return "task-123"

    monkeypatch.setattr(backup.globus, "submit", fake_submit)

    task_id = backup.submit(cfg_for(), [{"DATA_TYPE": "transfer_item"}])

    assert task_id == "task-123"
    assert seen["delete_destination_extra"] is True
    assert seen["sync_level"] == 2
    assert seen["skip_source_errors"] is True


def test_a_dry_run_submits_nothing_and_returns_the_document(tmp_path, monkeypatch):
    _workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(
        backup, "submit", lambda *a, **k: pytest_fail("dry run must not submit")
    )
    monkeypatch.setattr("awm.dvc.config.load", cfg_for)

    result = backup.backup(dry_run=True)

    assert result["dry_run"] is True
    assert result["transfer_items"]
    assert "task_id" not in result


def pytest_fail(msg):
    raise AssertionError(msg)


# --- symlinks ---------------------------------------------------------------
#
# The failure that wrote these: one `logs.latest` link, emitted as a
# non-recursive item, wedged a whole-workspace transfer at the scan stage —
# Globus resolved it, found a directory, and retried `IS_A_DIRECTORY` forever
# with every counter at zero. `skip_source_errors` does not cover a malformed
# item, only an unreadable source.


def test_a_symlink_to_a_directory_is_never_emitted(tmp_path):
    touch(tmp_path / "real" / "f")
    (tmp_path / "link").symlink_to(tmp_path / "real")

    items = backup.partition(str(tmp_path), [], "/dest")

    assert sources(items) == {f"{tmp_path}/real/"}


def test_a_symlink_to_a_file_is_skipped_too(tmp_path):
    """Its target is backed up on its own account; the link would duplicate."""
    touch(tmp_path / "real.txt", "x")
    (tmp_path / "alias.txt").symlink_to(tmp_path / "real.txt")

    items = backup.partition(str(tmp_path), [], "/dest")

    assert sources(items) == {f"{tmp_path}/real.txt"}


def test_a_symlink_cannot_smuggle_an_excluded_tree_back_in(tmp_path):
    """`.awm/data` points at the checkouts this job exists to leave alone."""
    touch(tmp_path / "data" / "chunk" / "big.bin", "payload")
    touch(tmp_path / "keep.txt")
    (tmp_path / "awm").mkdir()
    (tmp_path / "awm" / "data").symlink_to(tmp_path / "data")

    items = backup.partition(str(tmp_path), [str(tmp_path / "data" / "chunk")],
                             "/dest")

    assert not any("awm/data" in s for s in sources(items))
    assert f"{tmp_path}/keep.txt" in sources(items)


def test_a_symlinked_dir_does_not_force_its_parent_to_be_walked(tmp_path):
    """A parent holding only a link still goes as one recursive item."""
    touch(tmp_path / "top" / "f")
    (tmp_path / "top" / "link").symlink_to(tmp_path / "top")

    items = backup.partition(str(tmp_path), [], "/dest")

    assert sources(items) == {f"{tmp_path}/top/"}
