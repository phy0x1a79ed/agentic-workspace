"""Run the OneDrive prelude's JavaScript against an in-memory SharePoint.

``fake_sharepoint.js`` replaces ``window.fetch`` in a headless Chromium page, so
these tests exercise the prelude's real request building and error coding. The
SharePoint semantics they lean on (a missing folder answers ``Exists: false``,
a missing file answers 404) are the fake's; the live run on mira proves the real
service still behaves that way. Skipped where playwright or its browser is
missing.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from mira_api.storage_drivers import ONEDRIVE_PRELUDE, _call, classify

sync_api = pytest.importorskip("playwright.sync_api")

FAKE_SP = (Path(__file__).parent / "fake_sharepoint.js").read_text()


@pytest.fixture(scope="module")
def browser():
    try:
        pw = sync_api.sync_playwright().start()
        b = pw.chromium.launch()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no headless chromium: {exc}")
    yield b
    b.close()
    pw.stop()


class SP:
    """One fresh page with the fake installed; ``call`` runs a prelude helper."""

    def __init__(self, page):
        self.page = page

    def reset(self, **spec):
        self.page.evaluate(f"window.__fakeSP.reset({json.dumps(spec)})")

    def call(self, helper: str, *args):
        expr = ONEDRIVE_PRELUDE + "\n" + _call(f"window.__awmMira.onedrive.{helper}", *args)
        try:
            return json.loads(self.page.evaluate(expr))
        except sync_api.Error as exc:
            raise classify(exc) from exc

    def nodes(self) -> dict:
        return self.page.evaluate(
            "Object.fromEntries(Object.entries(window.__fakeSP.nodes)"
            ".map(([k, n]) => [k, n.kind]))")

    def set(self, key: str, value):
        self.page.evaluate(f"window.__fakeSP[{json.dumps(key)}] = {json.dumps(value)}")


@pytest.fixture()
def sp(browser):
    page = browser.new_page()
    page.evaluate(FAKE_SP)
    yield SP(page)
    page.close()


D = "/teams/x/Docs"


class TestErrors:
    def test_ls_missing_is_enoent(self, sp):
        with pytest.raises(FileNotFoundError):
            sp.call("ls", f"{D}/nope")

    def test_ls_file_is_enotdir(self, sp):
        sp.reset(files={f"{D}/a.txt": "hi"})
        with pytest.raises(NotADirectoryError):
            sp.call("ls", f"{D}/a.txt")

    def test_ls_empty_folder_is_empty(self, sp):
        sp.reset(folders=[f"{D}/empty"])
        assert sp.call("ls", f"{D}/empty") == []

    def test_ls_unknown_site_is_enoent(self, sp):
        with pytest.raises(FileNotFoundError):
            sp.call("ls", "/teams/nope/Docs")

    def test_stat_missing_is_enoent(self, sp):
        with pytest.raises(FileNotFoundError):
            sp.call("stat", f"{D}/nope")

    def test_get_folder_is_eisdir(self, sp):
        sp.reset(folders=[f"{D}/f"])
        with pytest.raises(IsADirectoryError):
            sp.call("get", f"{D}/f")

    def test_signin_redirect_is_eauth(self, sp):
        sp.set("auth", "redirect")
        with pytest.raises(PermissionError, match="sign-in"):
            sp.call("ls", D)

    def test_403_is_eauth(self, sp):
        sp.set("auth", "403")
        with pytest.raises(PermissionError, match="403"):
            sp.call("stat", D)


class TestListing:
    def test_ls_lists_folders_then_files_without_forms(self, sp):
        sp.reset(folders=[f"{D}/sub", f"{D}/Forms"], files={f"{D}/a.txt": "hi"})
        out = sp.call("ls", D)
        assert [(e["name"], e["is_dir"]) for e in out] == [("sub", True), ("a.txt", False)]
        assert out[1]["size"] == 2

    def test_names_with_url_specials_round_trip(self, sp):
        name = f"{D}/it's #1 & 50%?.txt"
        sp.reset(files={name: "x"})
        assert sp.call("stat", name)["path"] == name


class TestMkdirAndPut:
    def test_mkdir_creates_each_level_and_is_idempotent(self, sp):
        e = sp.call("mkdir", f"{D}/a/b/c")
        assert e["path"] == f"{D}/a/b/c" and e["is_dir"]
        assert sp.call("mkdir", f"{D}/a/b/c")["path"] == f"{D}/a/b/c"
        nodes = sp.nodes()
        assert all(nodes[p] == "folder" for p in (f"{D}/a", f"{D}/a/b", f"{D}/a/b/c"))

    def test_mkdir_through_a_file_is_enotdir(self, sp):
        sp.reset(files={f"{D}/a": "x"})
        with pytest.raises(NotADirectoryError):
            sp.call("mkdir", f"{D}/a/b")

    def test_mkdir_missing_library_is_enoent(self, sp):
        with pytest.raises(FileNotFoundError, match="document library"):
            sp.call("mkdir", "/teams/x/NoLib/a")

    def test_mkdir_above_library_is_einval(self, sp):
        with pytest.raises(ValueError):
            sp.call("mkdir", "/teams/x")

    def test_put_into_missing_folder_says_parents(self, sp):
        b64 = base64.b64encode(b"data").decode()
        with pytest.raises(FileNotFoundError, match="parents"):
            sp.call("put", f"{D}/new/a.bin", b64, None, False)

    def test_put_with_parents_creates_folders(self, sp):
        b64 = base64.b64encode(b"\x00\x01\x02").decode()
        e = sp.call("put", f"{D}/new/deep/a.bin", b64, None, True)
        assert e["size"] == 3 and e["path"] == f"{D}/new/deep/a.bin"
        got = sp.call("get", f"{D}/new/deep/a.bin")
        assert base64.b64decode(got["b64"]) == b"\x00\x01\x02"
        assert got["filename"] == "a.bin"


class TestRm:
    def test_rm_file_and_folder(self, sp):
        sp.reset(folders=[f"{D}/f"], files={f"{D}/f/a.txt": "x", f"{D}/b.txt": "y"})
        sp.call("rm", f"{D}/b.txt")
        sp.call("rm", f"{D}/f")
        assert sp.call("ls", D) == []

    def test_rm_missing_is_enoent(self, sp):
        with pytest.raises(FileNotFoundError):
            sp.call("rm", f"{D}/nope")

    def test_rm_refuses_a_library(self, sp):
        with pytest.raises(ValueError):
            sp.call("rm", D)
