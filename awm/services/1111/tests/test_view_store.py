import os
import sys
import time

import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "view"))

import store as store_mod  # noqa: E402
from store import Store, StoreError  # noqa: E402

FIELDS = "Steps: 20, Sampler: Euler, CFG scale: 7, Seed: 1, Size: 8x8"


def _png(path, mtime, text="p"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    info = PngInfo()
    info.add_text("parameters", f"{text}\n{FIELDS}")
    Image.new("RGB", (8, 8)).save(path, pnginfo=info)
    os.utime(path, (mtime, mtime))


@pytest.fixture
def env(tmp_path):
    out = tmp_path / "outputs"
    old = time.time() - 100
    _png(str(out / "txt2img-images/2024-01-01/00000-1.png"), old)
    _png(str(out / "txt2img-images/2024-01-01/00001-2.png"), old + 1)
    _png(str(out / "txt2img-images/2024-01-02/00000-3.png"), old + 2)
    _png(str(out / "img2img-images/00000-4.png"), old + 3)
    (out / "img2img-images/00000-4.txt").write_text("sidecar")
    s = Store(str(out), str(tmp_path / "data"))
    s.scan()
    yield s, out
    s.close()


def _ids(s, source):
    return [i["id"] for i in s.list_images(source)["items"]]


def _id_of(s, name):
    return next(i["id"] for i in s.list_images("dir:")["items"] if i["name"] == name)


def test_tree_counts(env):
    s, _ = env
    tree = {t["path"]: (t["direct"], t["total"]) for t in s.tree()}
    assert tree[""] == (0, 4)
    assert tree["txt2img-images"] == (0, 3)
    assert tree["txt2img-images/2024-01-01"] == (2, 2)
    assert tree["img2img-images"] == (1, 1)


def test_listing_newest_first_and_recursive(env):
    s, _ = env
    names = [i["name"] for i in s.list_images("dir:txt2img-images")["items"]]
    assert names == ["00000-3.png", "00001-2.png", "00000-1.png"]
    page = s.list_images("dir:", offset=1, limit=2)
    assert page["total"] == 4 and len(page["items"]) == 2


def test_dir_prefix_does_not_match_sibling(env):
    s, out = env
    _png(str(out / "txt2img-images/2024-01-01x/a.png"), time.time() - 50)
    s.scan()
    assert len(_ids(s, "dir:txt2img-images/2024-01-01")) == 2


def test_rescan_is_incremental_and_marks_missing(env):
    s, out = env
    assert s.scan() == {"added": 0, "updated": 0, "missing": 0}
    os.remove(out / "txt2img-images/2024-01-02/00000-3.png")
    assert s.scan()["missing"] == 1
    assert s.list_images("dir:")["total"] == 3


def test_scan_skips_files_still_being_written(env):
    s, out = env
    _png(str(out / "txt2img-images/2024-01-02/fresh.png"), time.time())
    assert s.scan()["added"] == 0
    assert s.scan()["missing"] == 0


def test_corrupt_file_is_indexed_with_error(env):
    s, out = env
    bad = out / "txt2img-images/2024-01-02/bad.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n junk")
    os.utime(bad, (time.time() - 50,) * 2)
    s.scan()
    img = s.get_image(_id_of(s, "bad.png"))
    assert img["error"] and img["meta"] is None


def test_folder_membership_is_many_to_many(env):
    s, _ = env
    a = s.create_folder("A")
    b = s.create_folder("B")
    img = _id_of(s, "00000-1.png")
    s.add_to_folder(a, [img])
    s.add_to_folder(b, [img])
    s.add_to_folder(b, [img])
    assert _ids(s, f"folder:{a}") == [img] and _ids(s, f"folder:{b}") == [img]
    assert sorted(s.get_image(img)["folders"]) == [a, b]
    assert s.list_images("dir:")["total"] == 4


def test_move_between_folders(env):
    s, _ = env
    a, b = s.create_folder("A"), s.create_folder("B")
    img = _id_of(s, "00000-1.png")
    s.add_to_folder(a, [img])
    s.move_between_folders(a, b, [img])
    assert _ids(s, f"folder:{a}") == [] and _ids(s, f"folder:{b}") == [img]
    s.remove_from_folder(b, [img])
    assert _ids(s, f"folder:{b}") == []


def test_folder_names_rename_nest_and_cycles(env):
    s, _ = env
    a = s.create_folder("A")
    with pytest.raises(StoreError):
        s.create_folder("A")
    child = s.create_folder("A", parent_id=a)
    s.rename_folder(child, "C")
    with pytest.raises(StoreError):
        s.reparent_folder(a, child)
    with pytest.raises(StoreError):
        s.rename_folder(child, " ")
    s.reparent_folder(child, None)
    assert {f["name"]: f["parent_id"] for f in s.folders()} == {"A": None, "C": None}


def test_delete_folder_cascades_but_keeps_images(env):
    s, out = env
    a = s.create_folder("A")
    child = s.create_folder("sub", parent_id=a)
    img = _id_of(s, "00000-1.png")
    s.add_to_folder(child, [img])
    s.delete_folder(a)
    assert s.folders() == []
    assert s.get_image(img)["folders"] == []
    assert (out / "txt2img-images/2024-01-01/00000-1.png").exists()


def test_trash_restore_keeps_memberships_and_sidecar(env):
    s, out = env
    a = s.create_folder("A")
    img = _id_of(s, "00000-4.png")
    s.add_to_folder(a, [img])
    assert s.trash([img]) == 1
    assert not (out / "img2img-images/00000-4.png").exists()
    assert not (out / "img2img-images/00000-4.txt").exists()
    assert _ids(s, "trash") == [img] and _ids(s, f"folder:{a}") == []
    assert os.path.isfile(s.file_path(img))
    assert s.scan()["missing"] == 0
    assert s.restore([img]) == {"restored": 1, "skipped": []}
    assert (out / "img2img-images/00000-4.txt").exists()
    assert _ids(s, f"folder:{a}") == [img]
    assert s.trash_count() == 0


def test_restore_refuses_to_overwrite(env):
    s, out = env
    img = _id_of(s, "00000-1.png")
    s.trash([img])
    _png(str(out / "txt2img-images/2024-01-01/00000-1.png"), time.time() - 50)
    assert s.restore([img]) == {"restored": 0, "skipped": [img]}


def test_purge_deletes_files_and_rows(env):
    s, out = env
    a = s.create_folder("A")
    ids = [_id_of(s, "00000-1.png"), _id_of(s, "00001-2.png")]
    s.add_to_folder(a, ids)
    s.trash(ids)
    assert s.purge([ids[0]]) == 1
    assert s.purge() == 1
    assert os.listdir(s.trash_dir) == []
    assert s.list_images("dir:")["total"] == 2
    with pytest.raises(StoreError):
        s.get_image(ids[0])
    assert s.folders()[0]["count"] == 0


def test_like_prefix_escapes_wildcards():
    assert store_mod._like_prefix("a_b%") == "a\\_b\\%/%"
