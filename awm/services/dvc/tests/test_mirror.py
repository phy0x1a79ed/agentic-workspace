"""Tree partitioning: cover everything except the DVC checkouts, exactly."""

from __future__ import annotations

from awm.dvc import mirror


def touch(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def sources(items):
    return {i["source_path"] for i in items}


def test_a_clean_tree_is_emitted_as_whole_recursive_dirs(tmp_path):
    touch(tmp_path / "a" / "deep" / "f")
    touch(tmp_path / "b" / "g")

    items = mirror.partition(str(tmp_path), [], "/dest")

    # Nothing excluded beneath them, so each top-level dir goes as one item
    # rather than being walked into.
    assert sources(items) == {f"{tmp_path}/a/", f"{tmp_path}/b/"}
    assert all(i["recursive"] for i in items)


def test_an_excluded_path_is_dropped_and_its_siblings_survive(tmp_path):
    touch(tmp_path / "proj" / "assembly" / "out")
    touch(tmp_path / "proj" / "keep" / "src")

    items = mirror.partition(str(tmp_path), [str(tmp_path / "proj" / "assembly")], "/dest")

    assert sources(items) == {f"{tmp_path}/proj/keep/"}


def test_a_name_shared_with_an_excluded_dir_elsewhere_is_not_dropped(tmp_path):
    # The reason filter_rules cannot be used: they match an item's *name* at any
    # depth, so excluding the DVC output "assembly" would also delete the
    # unrelated source directory that happens to share the name.
    touch(tmp_path / "data" / "assembly" / "out.bam")
    touch(tmp_path / "src" / "transforms" / "assembly" / "run.py")

    items = mirror.partition(str(tmp_path), [str(tmp_path / "data" / "assembly")], "/dest")

    assert f"{tmp_path}/src/" in sources(items)
    assert not any("data/assembly" in s for s in sources(items))


def test_destination_paths_are_rebased_under_the_prefix(tmp_path):
    touch(tmp_path / "keep" / "f")
    touch(tmp_path / "drop" / "f")

    items = mirror.partition(str(tmp_path), [str(tmp_path / "drop")], "/backups/node")

    assert [i["destination_path"] for i in items] == ["/backups/node/keep/"]


def test_a_lone_file_beside_an_exclusion_is_emitted_non_recursively(tmp_path):
    touch(tmp_path / "top.txt")
    touch(tmp_path / "drop" / "f")

    items = mirror.partition(str(tmp_path), [str(tmp_path / "drop")], "/dest")

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

    items = mirror.partition(
        str(tmp_path), [str(tmp_path / "a" / "b" / "c" / "excluded")], "/dest"
    )

    assert sources(items) == {
        f"{tmp_path}/a/b/sibling/",
        f"{tmp_path}/a/other/",
    }


def test_dvc_output_paths_collects_every_out_not_just_the_first(tmp_path):
    touch(
        tmp_path / "proj" / "multi.dvc",
        "outs:\n- md5: aaa\n  path: first\n- md5: bbb\n  path: second\n",
    )

    assert mirror.dvc_output_paths(tmp_path) == [
        str(tmp_path / "proj" / "first"),
        str(tmp_path / "proj" / "second"),
    ]


def test_dvc_output_paths_resolves_relative_to_the_pin_not_the_root(tmp_path):
    touch(tmp_path / "deep" / "nest" / "d.dvc", "outs:\n- md5: aaa\n  path: out\n")

    assert mirror.dvc_output_paths(tmp_path) == [str(tmp_path / "deep" / "nest" / "out")]


def test_an_unparseable_pin_does_not_abort_discovery(tmp_path):
    touch(tmp_path / "bad.dvc", "outs:\n- md5: [unclosed\n")
    touch(tmp_path / "good.dvc", "outs:\n- md5: aaa\n  path: out\n")

    assert mirror.dvc_output_paths(tmp_path) == [str(tmp_path / "out")]
