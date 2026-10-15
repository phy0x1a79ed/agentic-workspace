"""Tests for the storage drivers' Python layer.

The in-page SharePoint JavaScript is verified live on mira (it must run against
the real logged-in session); these tests cover the Python side — which helper
each op invokes, the args threaded through, and the passthrough of the JS
return value — using a fake CDP page (the same pattern as ``test_drivers.py``).
"""

from __future__ import annotations

import json

import pytest

from mira_api.storage_drivers import (
    STORAGE_REGISTRY, TRANSFER_TIMEOUT_S, OneDriveDriver)

pytestmark = pytest.mark.asyncio


class FakePage:
    """Stands in for CDPPage: returns canned data keyed by the helper called."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.exprs: list[str] = []
        self.kw: list[dict] = []

    async def evaluate_json(self, expr: str, **kw):
        self.exprs.append(expr)
        self.kw.append(kw)
        for helper, value in self.responses.items():
            if helper in expr:
                return value
        return None


class TestOneDriveDriver:
    def test_registered(self):
        assert STORAGE_REGISTRY["onedrive"] is OneDriveDriver
        assert OneDriveDriver.target_needle == ".sharepoint.com"

    def test_create_builds_tenant_and_personal_pages(self):
        d = OneDriveDriver.create("http://cdp")
        assert set(d.pages()) == {"tenant", "personal"}
        assert d.page.needle == ".sharepoint.com"
        assert d.personal_page.needle == "-my.sharepoint.com"

    async def test_personal_paths_use_the_personal_page(self):
        tenant = FakePage({"window.__awmMira.onedrive.ls": [{"name": "t"}]})
        personal = FakePage({"window.__awmMira.onedrive.ls": [{"name": "p"}]})
        d = OneDriveDriver(tenant, personal)
        assert (await d.ls("/personal/me_ubc_ca/Documents"))[0]["name"] == "p"
        assert (await d.ls("/teams/x/Docs"))[0]["name"] == "t"

    async def test_personal_path_without_page_fails_alone(self):
        d = OneDriveDriver(FakePage({"window.__awmMira.onedrive.ls": []}))
        assert await d.ls("/teams/x/Docs") == []
        with pytest.raises(RuntimeError, match="-my.sharepoint.com"):
            await d.ls("/personal/me/Documents")

    async def test_coded_js_errors_are_classified(self):
        from mira_api.cdp import CDPError

        class _Throwing(FakePage):
            async def evaluate_json(self, expr, **kw):
                raise CDPError("JS error: Error: ENOENT: /teams/x/a: not found\n    at O.ls")

        with pytest.raises(FileNotFoundError, match="^/teams/x/a: not found$"):
            await OneDriveDriver(_Throwing({})).stat("/teams/x/a")

    async def test_uncoded_errors_pass_through(self):
        from mira_api.cdp import CDPError

        class _Throwing(FakePage):
            async def evaluate_json(self, expr, **kw):
                raise CDPError("ls: timed out")

        with pytest.raises(CDPError):
            await OneDriveDriver(_Throwing({})).ls("/teams/x")

    async def test_ls_passthrough_and_path_arg(self):
        page = FakePage({"window.__awmMira.onedrive.ls": [
            {"name": "sub", "path": "/teams/x/Docs/sub", "is_dir": True},
            {"name": "a.txt", "path": "/teams/x/Docs/a.txt", "is_dir": False,
             "size": 4}]})
        out = await OneDriveDriver(page).ls("/teams/x/Docs")
        assert [e["name"] for e in out] == ["sub", "a.txt"]
        assert json.dumps("/teams/x/Docs") in page.exprs[-1]
        assert "window.__awmMira.onedrive.ls" in page.exprs[-1]

    async def test_ls_none_becomes_empty(self):
        page = FakePage({})  # helper returns None
        assert await OneDriveDriver(page).ls("/x") == []

    async def test_stat_passthrough(self):
        page = FakePage({"window.__awmMira.onedrive.stat":
                         {"name": "a.txt", "path": "/teams/x/a.txt",
                          "is_dir": False, "size": 5, "id": "U1"}})
        out = await OneDriveDriver(page).stat("/teams/x/a.txt")
        assert out["id"] == "U1" and out["is_dir"] is False

    async def test_get_passthrough(self):
        page = FakePage({"window.__awmMira.onedrive.get":
                         {"filename": "a.txt", "mime": "text/plain", "b64": "aGk="}})
        out = await OneDriveDriver(page).get("/teams/x/a.txt", 8, 4)
        assert out == {"filename": "a.txt", "mime": "text/plain", "b64": "aGk="}
        assert '"/teams/x/a.txt", 8, 4, null)' in page.exprs[-1]
        assert page.kw[-1]["timeout"] == TRANSFER_TIMEOUT_S

    async def test_upload_never_retries(self):
        page = FakePage({"window.__awmMira.onedrive.upload": {"offset": 4}})
        out = await OneDriveDriver(page).upload(
            "/teams/x/big.bin", "uid", 0, False, "AAAA", True)
        assert out == {"offset": 4}
        assert '"/teams/x/big.bin", "uid", 0, false, "AAAA", true)' in page.exprs[-1]
        assert page.kw[-1] == {"timeout": TRANSFER_TIMEOUT_S, "retry": False}

    async def test_upload_cancel(self):
        page = FakePage({"window.__awmMira.onedrive.uploadCancel": {"ok": True}})
        await OneDriveDriver(page).upload_cancel("/teams/x/big.bin", "uid")
        assert "onedrive.uploadCancel" in page.exprs[-1]

    async def test_put_threads_path_b64_mime(self):
        page = FakePage({"window.__awmMira.onedrive.put":
                         {"name": "up.bin", "path": "/teams/x/up.bin",
                          "is_dir": False, "size": 3}})
        out = await OneDriveDriver(page).put(
            "/teams/x/up.bin", "AAEC", "application/octet-stream")
        assert out["name"] == "up.bin"
        expr = page.exprs[-1]
        assert json.dumps("/teams/x/up.bin") in expr
        assert json.dumps("AAEC") in expr
        assert json.dumps("application/octet-stream") in expr

    async def test_rm_invokes_helper_with_path(self):
        page = FakePage({"window.__awmMira.onedrive.rm": {"ok": True}})
        await OneDriveDriver(page).rm("/teams/x/old.txt")
        assert "window.__awmMira.onedrive.rm" in page.exprs[-1]
        assert json.dumps("/teams/x/old.txt") in page.exprs[-1]

    async def test_search_threads_query_limit_root(self):
        page = FakePage({"window.__awmMira.onedrive.search": [
            {"name": "hit.txt", "path": "/teams/x/Docs/hit.txt",
             "is_dir": False}]})
        out = await OneDriveDriver(page).search("hit", 10, "/teams/x/Docs")
        assert [e["name"] for e in out] == ["hit.txt"]
        expr = page.exprs[-1]
        assert json.dumps("hit") in expr and json.dumps("/teams/x/Docs") in expr
