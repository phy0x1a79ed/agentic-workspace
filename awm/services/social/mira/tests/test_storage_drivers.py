"""Tests for the storage drivers' Python layer.

The in-page SharePoint JavaScript is verified live on mira (it must run against
the real logged-in session); these tests cover the Python side — which helper
each op invokes, the args threaded through, and the passthrough of the JS
return value — using a fake CDP page (the same pattern as ``test_drivers.py``).
"""

from __future__ import annotations

import json

import pytest

from mira_api.storage_drivers import STORAGE_REGISTRY, OneDriveDriver

pytestmark = pytest.mark.asyncio


class FakePage:
    """Stands in for CDPPage: returns canned data keyed by the helper called."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.exprs: list[str] = []

    async def evaluate_json(self, expr: str):
        self.exprs.append(expr)
        for helper, value in self.responses.items():
            if helper in expr:
                return value
        return None


class TestOneDriveDriver:
    def test_registered(self):
        assert STORAGE_REGISTRY["onedrive"] is OneDriveDriver
        assert OneDriveDriver.target_needle == "sharepoint.com"

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
        out = await OneDriveDriver(page).get("/teams/x/a.txt")
        assert out == {"filename": "a.txt", "mime": "text/plain", "b64": "aGk="}
        assert json.dumps("/teams/x/a.txt") in page.exprs[-1]

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
