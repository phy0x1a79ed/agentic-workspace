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
import uuid
from pathlib import Path

import pytest

from mira_api.storage_drivers import ONEDRIVE_PRELUDE, OneDriveDriver, _call, classify

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


    def test_ls_past_the_threshold_pages_the_list_view(self, sp):
        files = {f"{D}/big/f{i:02d}.txt": "x" * i for i in range(7)}
        sp.reset(folders=[f"{D}/big", f"{D}/big/sub"], files=files)
        sp.set("threshold", 3)
        sp.set("pageSize", 3)
        rows = sp.call("ls", f"{D}/big")
        assert [r["name"] for r in rows] == ["sub"] + [f"f{i:02d}.txt" for i in range(7)]
        assert rows[0]["is_dir"] and rows[3]["size"] == 2
        assert not rows[1]["id"].startswith("{")
        calls = sp.page.evaluate("window.__fakeSP.calls")
        assert sum("RenderListDataAsStream" in c for c in calls) == 3


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


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


UID = "0f8fad5b-d9cb-469f-a165-70867728950e"


class TestChunkedUpload:
    def test_three_chunks_assemble_one_file(self, sp):
        sp.reset(folders=[f"{D}/up"])
        path = f"{D}/up/big.bin"
        assert sp.call("upload", path, UID, 0, False, _b64(b"aaaa"), False) == {"offset": 4}
        assert sp.call("upload", path, UID, 4, False, _b64(b"bbb"), False) == {"offset": 7}
        out = sp.call("upload", path, UID, 7, True, _b64(b"cc"), False)
        assert out["entry"]["size"] == 9 and out["entry"]["path"] == path
        got = sp.call("get", path, 0, None)
        assert base64.b64decode(got["b64"]) == b"aaaabbbcc"

    def test_first_chunk_creates_parents(self, sp):
        path = f"{D}/new/deep/big.bin"
        sp.call("upload", path, UID, 0, False, _b64(b"aa"), True)
        assert sp.nodes()[f"{D}/new/deep"] == "folder"

    def test_single_final_chunk_is_a_plain_put(self, sp):
        out = sp.call("upload", f"{D}/one.bin", UID, 0, True, _b64(b"xyz"), False)
        assert out["entry"]["size"] == 3
        assert not any("StartUpload" in c for c in sp.page.evaluate("window.__fakeSP.calls"))

    def test_wrong_offset_fails_and_cancels(self, sp):
        path = f"{D}/big.bin"
        sp.call("upload", path, UID, 0, False, _b64(b"aaaa"), False)
        with pytest.raises(Exception, match="offset"):
            sp.call("upload", path, UID, 9, False, _b64(b"b"), False)
        assert sp.page.evaluate("Object.keys(window.__fakeSP.uploads).length") == 0

    def test_bad_upload_id_is_einval(self, sp):
        with pytest.raises(ValueError, match="GUID"):
            sp.call("upload", f"{D}/big.bin", "x'); evil('", 0, False, _b64(b"a"), False)


class TestRangedGet:
    DATA = bytes(range(256)) * 4

    def _read(self, sp, path, step):
        out, offset = b"", 0
        while True:
            got = sp.call("get", path, offset, step)
            chunk = base64.b64decode(got["b64"])
            out += chunk
            offset += len(chunk)
            if not chunk or offset >= got["size"]:
                return out, got

    @pytest.mark.parametrize("mode,expect_call", [
        ("value", "$value"), ("download", "download.aspx"), ("none", "$value")])
    def test_chunked_read_round_trips(self, sp, mode, expect_call):
        sp.reset(files={f"{D}/r.bin": ""})
        sp.page.evaluate(
            f"window.__fakeSP.nodes[{json.dumps(D + '/r.bin')}].data = "
            f"new Uint8Array({json.dumps(list(self.DATA))})")
        sp.set("range", mode)
        out, got = self._read(sp, f"{D}/r.bin", 300)
        assert out == self.DATA and got["size"] == len(self.DATA) and got["etag"]
        assert any(expect_call in c for c in sp.page.evaluate("window.__fakeSP.calls"))

    def test_offset_past_end_is_empty(self, sp):
        sp.reset(files={f"{D}/a.txt": "hi"})
        got = sp.call("get", f"{D}/a.txt", 10, 5)
        assert got["b64"] == "" and got["size"] == 2


class _SyncPage:
    """A CDPPage stand-in that evaluates on the fake's page synchronously."""

    def __init__(self, page):
        self.page = page

    async def evaluate_json(self, expr, **_kw):
        try:
            value = self.page.evaluate(expr)
        except sync_api.Error as exc:
            raise RuntimeError(str(exc)) from exc
        return json.loads(value) if isinstance(value, str) else value


def _drive(coro):
    """Run a driver coroutine without an event loop. Sync playwright refuses a
    running loop, and the driver awaits nothing but the page and sleep(0)."""
    try:
        while True:
            coro.send(None)
    except StopIteration as stop:
        return stop.value


@pytest.fixture()
def drv(sp):
    d = OneDriveDriver(_SyncPage(sp.page), _SyncPage(sp.page))
    d.poll_delays = (0,)
    return d


E = "/teams/y/Docs"


class TestMoveCopy:
    def test_rename_keeps_the_item(self, sp, drv):
        sp.reset(files={f"{D}/a.txt": "hi"})
        before = sp.call("stat", f"{D}/a.txt")["id"]
        entry = _drive(drv.mv(f"{D}/a.txt", f"{D}/b.txt"))
        assert entry["name"] == "b.txt" and entry["id"] == before
        assert f"{D}/a.txt" not in sp.nodes()

    def test_move_folder_takes_its_tree(self, sp, drv):
        sp.reset(folders=[f"{D}/s", f"{D}/t"], files={f"{D}/s/f.txt": "x"})
        entry = _drive(drv.mv(f"{D}/s", f"{D}/t/s2"))
        assert entry["is_dir"] is True
        assert sp.nodes().get(f"{D}/t/s2/f.txt") == "file"
        assert f"{D}/s" not in sp.nodes()

    def test_copy_folder_leaves_the_source(self, sp, drv):
        sp.reset(folders=[f"{D}/s"], files={f"{D}/s/f.txt": "x"})
        entry = _drive(drv.cp(f"{D}/s", f"{D}/s-copy"))
        assert entry["id"] != sp.call("stat", f"{D}/s")["id"]
        assert {f"{D}/s/f.txt", f"{D}/s-copy/f.txt"} <= set(sp.nodes())

    def test_existing_target_needs_overwrite(self, sp, drv):
        sp.reset(files={f"{D}/a.txt": "new", f"{D}/b.txt": "old"})
        with pytest.raises(FileExistsError, match="overwrite"):
            _drive(drv.cp(f"{D}/a.txt", f"{D}/b.txt"))
        _drive(drv.cp(f"{D}/a.txt", f"{D}/b.txt", overwrite=True))
        got = sp.call("get", f"{D}/b.txt", 0, None)
        assert base64.b64decode(got["b64"]) == b"new"

    def test_missing_parent_needs_parents(self, sp, drv):
        sp.reset(files={f"{D}/a.txt": "hi"})
        with pytest.raises(FileNotFoundError, match="parents"):
            _drive(drv.mv(f"{D}/a.txt", f"{D}/n/m/a.txt"))
        _drive(drv.mv(f"{D}/a.txt", f"{D}/n/m/a.txt", parents=True))
        assert sp.nodes().get(f"{D}/n/m/a.txt") == "file"

    def test_missing_source_is_enoent(self, sp, drv):
        with pytest.raises(FileNotFoundError):
            _drive(drv.mv(f"{D}/nope", f"{D}/b"))

    def test_into_itself_is_einval(self, sp, drv):
        sp.reset(folders=[f"{D}/s"])
        with pytest.raises(ValueError):
            _drive(drv.mv(f"{D}/s", f"{D}/s/inner"))

    @pytest.mark.parametrize("op,source_left", [("cp", True), ("mv", False)])
    def test_cross_site_runs_a_copy_job(self, sp, drv, op, source_left):
        sp.reset(sites=["/teams/x", "/teams/y"], libraries=[D, E],
                 folders=[f"{D}/s"], files={f"{D}/s/f.txt": "x"})
        entry = _drive(getattr(drv, op)(f"{D}/s", f"{E}/s"))
        assert entry["path"] == f"{E}/s"
        assert sp.nodes().get(f"{E}/s/f.txt") == "file"
        assert (f"{D}/s" in sp.nodes()) is source_left

    def test_cross_site_rename_is_einval(self, sp, drv):
        sp.reset(sites=["/teams/x", "/teams/y"], libraries=[D, E],
                 files={f"{D}/a.txt": "x"})
        with pytest.raises(ValueError, match="keeps the name"):
            _drive(drv.cp(f"{D}/a.txt", f"{E}/b.txt"))

    def test_copy_job_error_is_reported(self, sp):
        sp.reset(sites=["/teams/x", "/teams/y"], libraries=[D, E],
                 files={f"{D}/a.txt": "x"})
        job = sp.call("copyJob", f"{D}/a.txt", E, False)
        assert sp.call("copyJobProgress", f"{D}/a.txt", job)["done"] is False
        sp.page.evaluate(f"window.__fakeSP.addFile('{E}/a.txt', 'taken')")
        prog = sp.call("copyJobProgress", f"{D}/a.txt", job)
        assert prog["done"] is True and "cannot place" in prog["error"]


class TestDiscovery:
    def test_entries_carry_web_urls(self, sp):
        sp.reset(folders=[f"{D}/a b"], files={f"{D}/a b/c#d.txt": "x"})
        rows = sp.call("ls", f"{D}/a b")
        assert rows[0]["web_url"].endswith("/teams/x/Docs/a%20b/c%23d.txt?web=1")
        folder = sp.call("stat", f"{D}/a b")
        assert folder["web_url"].endswith(
            "/teams/x/Docs/Forms/AllItems.aspx?id=%2Fteams%2Fx%2FDocs%2Fa%20b")

    def test_sites_merges_personal_search_and_followed(self, sp):
        sp.reset(searchSites=[
            {"Title": "X", "Path": "https://t.sharepoint.com/teams/x"},
            {"Title": "Y", "Path": "https://t.sharepoint.com/teams/y"}],
            followed=[{"Name": "Y", "Uri": "https://t.sharepoint.com/teams/y"},
                      {"Name": "Z", "Uri": "https://t.sharepoint.com/sites/z"}])
        rows = sp.call("sites", "")
        assert [(r["path"], r["kind"], r.get("followed", False)) for r in rows] == [
            ("/personal/me_x", "personal", False),
            ("/teams/x", "site", False),
            ("/teams/y", "site", True),
            ("/sites/z", "site", True)]

    def test_sites_with_a_site_lists_its_libraries(self, sp):
        sp.reset(sites=["/teams/x", "/teams/y"],
                 libraries=[D, "/teams/x/Shared Documents", "/teams/y/Docs"])
        rows = sp.call("sites", "/teams/x")
        assert sorted(r["path"] for r in rows) == [D, "/teams/x/Shared Documents"]
        assert all(r["kind"] == "library" and "AllItems.aspx" in r["web_url"] for r in rows)


ENGCYC = "c7be31b8-5867-4576-9500-3d8b45833463"


def _share_link(guid: str, prefix: bytes = b"\x00\x01", site: str = "x") -> str:
    token = base64.urlsafe_b64encode(prefix + uuid.UUID(guid).bytes_le + b"\x02\x03")
    return f"https://t.sharepoint.com/:f:/t/{site}/{token.decode().rstrip('=')}?e=abc"


class TestSharing:
    def test_organization_link_lists_and_revokes(self, sp):
        sp.reset(folders=[f"{D}/s"])
        made = sp.call("share", f"{D}/s", "organization", "view", [], False)
        assert made["url"].startswith("https://") and made["scope"] == "organization"
        info = sp.call("shares", f"{D}/s")
        assert [(l["url"], l["scope"], l["role"]) for l in info["links"]] == [
            (made["url"], "organization", "view")]
        sp.call("unshare", f"{D}/s", made["url"], "")
        assert sp.call("shares", f"{D}/s")["links"] == []

    def test_edit_link_on_a_file(self, sp):
        sp.reset(files={f"{D}/a.txt": "x"})
        made = sp.call("share", f"{D}/a.txt", "organization", "edit", [], False)
        link = sp.call("shares", f"{D}/a.txt")["links"][0]
        assert link["role"] == "edit" and link["share_id"] == made["share_id"]

    def test_refused_anyone_link_is_passed_through(self, sp):
        sp.reset(folders=[f"{D}/s"], denyAnyone=True)
        with pytest.raises(Exception, match="Anonymous sharing is disabled"):
            sp.call("share", f"{D}/s", "anyone", "view", [], False)

    def test_people_grant_never_mails_by_default_and_revokes(self, sp):
        sp.reset(folders=[f"{D}/s"])
        made = sp.call("share", f"{D}/s", "people", "edit", ["ann@x.ca"], False)
        assert made["people"] == ["ann@x.ca"]
        sent = sp.page.evaluate("window.__fakeSP.lastShareObject")
        assert sent["sendEmail"] is False and sent["roleValue"] == "role:1073741827"
        people = sp.call("shares", f"{D}/s")["people"]
        assert [(p["email"], p["role"]) for p in people] == [("ann@x.ca", "edit")]
        sp.call("unshare", f"{D}/s", "", "ann@x.ca")
        assert sp.call("shares", f"{D}/s")["people"] == []

    def test_unresolved_person_is_einval(self, sp):
        sp.reset(folders=[f"{D}/s"])
        with pytest.raises(ValueError, match="cannot resolve"):
            sp.call("share", f"{D}/s", "people", "view", ["nobody@x.ca"], False)

    def test_bad_scope_or_role_is_einval(self, sp):
        sp.reset(folders=[f"{D}/s"])
        with pytest.raises(ValueError):
            sp.call("share", f"{D}/s", "world", "view", [], False)
        with pytest.raises(ValueError):
            sp.call("share", f"{D}/s", "organization", "own", [], False)

    def test_unknown_link_is_enoent(self, sp):
        sp.reset(folders=[f"{D}/s"])
        with pytest.raises(FileNotFoundError):
            sp.call("unshare", f"{D}/s", "https://t/nope", "")


class TestResolve:
    def test_shares_api_resolves_a_made_link(self, sp):
        sp.reset(folders=[f"{D}/s"])
        made = sp.call("share", f"{D}/s", "organization", "view", [], False)
        got = sp.call("resolve", made["url"])
        assert got["via"] == "shares" and got["entry"]["path"] == f"{D}/s"

    @pytest.mark.parametrize("prefix", [b"\x00\x01", b"\x05"])
    def test_token_fallback_finds_the_unique_id(self, sp, prefix):
        sp.reset(folders=[f"{D}/Outputs", f"{D}/Outputs/Engcyc"],
                 uids={f"{D}/Outputs/Engcyc": ENGCYC}, sharesApi=False)
        got = sp.call("resolve", _share_link(ENGCYC, prefix))
        assert got["via"] == "token"
        assert got["entry"]["path"] == f"{D}/Outputs/Engcyc"
        assert got["entry"]["id"] == ENGCYC

    def test_unknown_token_is_enoent(self, sp):
        sp.reset(sharesApi=False)
        with pytest.raises(FileNotFoundError):
            sp.call("resolve", _share_link(str(uuid.uuid4())))

    def test_not_a_share_link_is_einval(self, sp):
        sp.reset(sharesApi=False)
        with pytest.raises(ValueError):
            sp.call("resolve", "https://t.sharepoint.com/teams/x/Docs")
