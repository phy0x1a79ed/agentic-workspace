"""Tests for the buckets module: registry/build, Google Drive (fake service),
OneDrive (stub mira daemon), and the hub bucket handlers.

The google client libraries are an optional dependency not installed in the
test env, so the Google Drive backend is exercised by injecting a fake Drive
``service`` (``_svc``) directly — ``_build_service`` (the only code that imports
google) is never reached. The two ops that lazily import ``googleapiclient.http``
(``get``/``put``) get a stub module injected into ``sys.modules``. The OneDrive
backend runs against a real aiohttp stub of the daemon's ``/v1/fs`` routes.
"""

from __future__ import annotations

import base64
import sys
import types

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


# --------------------------------------------------------------------------- #
# registry / build                                                            #
# --------------------------------------------------------------------------- #

class TestRegistry:
    def test_known_kinds_registered(self):
        from awm.social import buckets
        assert set(buckets.REGISTRY) == {"google_drive", "onedrive"}

    def test_build_google_drive(self):
        from awm.social import buckets
        from awm.social.config import BucketConfig

        b = buckets.build(BucketConfig(
            name="g", kind="google_drive", client_id="cid",
            client_secret="sec", refresh_token="rt", root="FOLDER"))
        assert b.kind == "google_drive"
        assert b._root_id == "FOLDER"
        assert b.account.client_id == "cid"

    def test_build_onedrive(self):
        from awm.social import buckets
        from awm.social.config import BucketConfig

        b = buckets.build(BucketConfig(
            name="o", kind="onedrive", source="mira",
            site="https://x.sharepoint.com", root="/teams/x/Docs",
            mira_url="https://m:7822", mira_token="tok"))
        assert b.kind == "onedrive"
        assert b._abs("") == "/teams/x/Docs"
        assert b._abs("a/b.txt") == "/teams/x/Docs/a/b.txt"

    def test_build_rejects_unknown_kind(self):
        from awm.social import buckets
        from awm.social.config import BucketConfig

        with pytest.raises(ValueError):
            buckets.build(BucketConfig(name="x", kind="dropbox"))


# --------------------------------------------------------------------------- #
# Google Drive (fake service)                                                 #
# --------------------------------------------------------------------------- #

class _Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class _FakeFiles:
    """A tiny in-memory Drive ``files()`` resource.

    Nodes are ``{id: {name, mimeType, parent, size, content}}`` rooted at
    ``"root"``. ``list`` understands the two query shapes the backend builds
    (``'<parent>' in parents`` and ``name contains '<q>'``).
    """

    _FOLDER = "application/vnd.google-apps.folder"

    def __init__(self, nodes):
        self.nodes = nodes
        self._seq = 0

    def list(self, *, q, fields=None, pageSize=None, pageToken=None, **kw):
        def run():
            if "in parents" in q:
                parent = q.split("'")[1]
                files = [self._meta(i) for i, n in self.nodes.items()
                         if n.get("parent") == parent]
                return {"files": files}
            if "name contains" in q:
                needle = q.split("'")[1]
                files = [self._meta(i) for i, n in self.nodes.items()
                         if needle in n.get("name", "")]
                return {"files": files}
            return {"files": []}
        return _Req(run)

    def _meta(self, fid):
        n = self.nodes[fid]
        m = {"id": fid, "name": n["name"], "mimeType": n["mimeType"]}
        if n.get("size") is not None:
            m["size"] = n["size"]
        if n.get("modifiedTime"):
            m["modifiedTime"] = n["modifiedTime"]
        return m

    def create(self, *, body, media_body=None, fields=None, **kw):
        def run():
            self._seq += 1
            fid = f"new{self._seq}"
            self.nodes[fid] = {
                "name": body["name"],
                "mimeType": body.get("mimeType", "text/plain"),
                "parent": body["parents"][0],
                "size": len(media_body._data) if media_body else 0,
                "content": media_body._data if media_body else b"",
            }
            return self._meta(fid)
        return _Req(run)

    def update(self, *, fileId, media_body=None, fields=None, **kw):
        def run():
            self.nodes[fileId]["content"] = media_body._data
            self.nodes[fileId]["size"] = len(media_body._data)
            return self._meta(fileId)
        return _Req(run)

    def delete(self, *, fileId, **kw):
        return _Req(lambda: self.nodes.pop(fileId, None))

    def get_media(self, *, fileId, **kw):
        return _Req(lambda: self.nodes[fileId]["content"])

    def export_media(self, *, fileId, mimeType, **kw):
        return _Req(lambda: b"EXPORTED:" + self.nodes[fileId]["content"])


class _FakeDrive:
    def __init__(self, nodes):
        self._files = _FakeFiles(nodes)

    def files(self):
        return self._files


@pytest.fixture()
def fake_google_http(monkeypatch):
    """Inject a stub ``googleapiclient.http`` so the lazy imports in get/put
    resolve without the real (uninstalled) library."""
    pkg = types.ModuleType("googleapiclient")
    http = types.ModuleType("googleapiclient.http")

    class MediaInMemoryUpload:
        def __init__(self, data, mimetype=None, resumable=False):
            self._data = data
            self.mimetype = mimetype

    class MediaIoBaseDownload:
        def __init__(self, buf, request):
            self._buf = buf
            self._data = request.execute()

        def next_chunk(self):
            self._buf.write(self._data)
            return (None, True)

    http.MediaInMemoryUpload = MediaInMemoryUpload
    http.MediaIoBaseDownload = MediaIoBaseDownload
    pkg.http = http
    monkeypatch.setitem(sys.modules, "googleapiclient", pkg)
    monkeypatch.setitem(sys.modules, "googleapiclient.http", http)
    return http


def _gdrive(nodes, root="root"):
    from awm.social.buckets.gdrive_bucket import GoogleDriveBucket
    from awm.social.buckets.base import Bucket_Account
    b = GoogleDriveBucket(Bucket_Account(
        name="g", kind="google_drive", root=root,
        client_id="c", client_secret="s", refresh_token="r"))
    b._svc = _FakeDrive(nodes)
    return b


_F = "application/vnd.google-apps.folder"


class TestGoogleDrive:
    def _tree(self):
        # root/  ├─ docs/ (folder)  │    └─ note.txt
        #        └─ pic.png
        return {
            "d1": {"name": "docs", "mimeType": _F, "parent": "root"},
            "f1": {"name": "note.txt", "mimeType": "text/plain", "parent": "d1",
                   "size": 5, "content": b"hello", "modifiedTime": "2026-01-01"},
            "f2": {"name": "pic.png", "mimeType": "image/png", "parent": "root",
                   "size": 3, "content": b"img"},
        }

    async def test_ls_root_sorts_folders_first(self):
        b = _gdrive(self._tree())
        entries = await b.ls("")
        assert [(e.name, e.is_dir) for e in entries] == [
            ("docs", True), ("pic.png", False)]
        assert entries[0].path == "docs" and entries[1].path == "pic.png"

    async def test_ls_subfolder(self):
        b = _gdrive(self._tree())
        entries = await b.ls("docs")
        assert [e.name for e in entries] == ["note.txt"]
        assert entries[0].path == "docs/note.txt"
        assert entries[0].size == 5

    async def test_stat_nested(self):
        b = _gdrive(self._tree())
        e = await b.stat("docs/note.txt")
        assert e.name == "note.txt" and not e.is_dir and e.id == "f1"

    async def test_missing_path_raises(self):
        b = _gdrive(self._tree())
        with pytest.raises(FileNotFoundError):
            await b.stat("docs/nope.txt")

    async def test_ambiguous_first_match_wins(self):
        nodes = self._tree()
        nodes["f1b"] = {"name": "note.txt", "mimeType": "text/plain",
                        "parent": "d1", "size": 9, "content": b"different"}
        b = _gdrive(nodes)
        e = await b.stat("docs/note.txt")     # two match; first wins, no raise
        assert e.name == "note.txt"

    async def test_get_downloads_bytes(self, fake_google_http):
        b = _gdrive(self._tree())
        name, mime, data = await b.get("docs/note.txt")
        assert name == "note.txt" and mime == "text/plain" and data == b"hello"

    async def test_get_exports_google_doc(self, fake_google_http):
        nodes = self._tree()
        nodes["g1"] = {"name": "plan", "parent": "root", "content": b"DOC",
                       "mimeType": "application/vnd.google-apps.document"}
        b = _gdrive(nodes)
        name, mime, data = await b.get("plan")
        assert name == "plan.pdf" and mime == "application/pdf"
        assert data == b"EXPORTED:DOC"

    async def test_get_on_folder_raises(self, fake_google_http):
        b = _gdrive(self._tree())
        with pytest.raises(IsADirectoryError):
            await b.get("docs")

    async def test_put_creates_new_file(self, fake_google_http):
        b = _gdrive(self._tree())
        e = await b.put("docs/fresh.txt", b"data", mime="text/plain")
        assert e.name == "fresh.txt" and e.path == "docs/fresh.txt"
        # visible on a re-list
        assert "fresh.txt" in {x.name for x in await b.ls("docs")}

    async def test_put_overwrites_existing(self, fake_google_http):
        b = _gdrive(self._tree())
        e = await b.put("docs/note.txt", b"replaced")
        assert e.id == "f1"          # same node updated, not a new one
        _n, _m, data = await b.get("docs/note.txt")
        assert data == b"replaced"

    async def test_rm_deletes(self):
        b = _gdrive(self._tree())
        await b.rm("pic.png")
        assert "pic.png" not in {e.name for e in await b.ls("")}

    async def test_mkdir_creates_nested(self):
        b = _gdrive(self._tree())
        e = await b.mkdir("a/b")
        assert e.name == "b" and e.is_dir
        # both levels exist now
        assert "a" in {x.name for x in await b.ls("")}
        assert "b" in {x.name for x in await b.ls("a")}

    async def test_search_name_contains(self):
        b = _gdrive(self._tree())
        hits = {e.name for e in await b.search("note")}
        assert hits == {"note.txt"}


# --------------------------------------------------------------------------- #
# OneDrive (stub mira daemon)                                                  #
# --------------------------------------------------------------------------- #

class _StubFsDaemon:
    """aiohttp stand-in for the daemon's /v1/fs/onedrive routes."""

    def __init__(self):
        self.calls = []          # (op, path-or-query)
        self.ls_entries = []
        self.get_file = {}
        self.put_received = None  # (path, bytes, mime)
        self.search_entries = []
        self.search_query = None
        self.rm_path = None
        self._runner = None
        self.base = ""

    async def start(self):
        from aiohttp import web
        app = web.Application()
        app.router.add_get("/v1/fs/{name}/ls", self._ls)
        app.router.add_get("/v1/fs/{name}/get", self._get)
        app.router.add_get("/v1/fs/{name}/search", self._search)
        app.router.add_post("/v1/fs/{name}/put", self._put)
        app.router.add_post("/v1/fs/{name}/rm", self._rm)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{list(self._runner.addresses)[0][1]}"

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()

    async def _ls(self, request):
        from aiohttp import web
        self.calls.append(("ls", request.query.get("path")))
        return web.json_response({"entries": self.ls_entries})

    async def _get(self, request):
        from aiohttp import web
        self.calls.append(("get", request.query.get("path")))
        return web.json_response(self.get_file)

    async def _search(self, request):
        from aiohttp import web
        self.search_query = dict(request.query)
        return web.json_response({"entries": self.search_entries})

    async def _put(self, request):
        from aiohttp import web
        body = await request.json()
        self.put_received = (
            body["path"], base64.b64decode(body["b64"]), body.get("mime"))
        return web.json_response({"entry": {
            "name": body["path"].rsplit("/", 1)[-1], "path": body["path"],
            "is_dir": False, "size": len(base64.b64decode(body["b64"]))}})

    async def _rm(self, request):
        from aiohttp import web
        body = await request.json()
        self.rm_path = body["path"]
        return web.json_response({"ok": True})


@pytest.fixture()
async def fs_daemon():
    d = _StubFsDaemon()
    await d.start()
    yield d
    await d.stop()


def _onedrive(daemon, root="/teams/x/Docs"):
    from awm.social.buckets.onedrive_bucket import OneDriveBucket
    from awm.social.buckets.base import Bucket_Account
    return OneDriveBucket(Bucket_Account(
        name="o", kind="onedrive", source="mira", root=root,
        site="https://x.sharepoint.com", mira_url=daemon.base,
        mira_token="tok", mira_verify_tls=False))


class TestOneDrive:
    async def test_ls_prefixes_root_and_strips_it_back(self, fs_daemon):
        fs_daemon.ls_entries = [
            {"name": "sub", "path": "/teams/x/Docs/sub", "is_dir": True},
            {"name": "a.txt", "path": "/teams/x/Docs/a.txt", "is_dir": False,
             "size": 4},
        ]
        b = _onedrive(fs_daemon)
        try:
            entries = await b.ls("")
            assert fs_daemon.calls[-1] == ("ls", "/teams/x/Docs")  # root sent absolute
            # paths come back relative to the bucket root
            assert [(e.name, e.path, e.is_dir) for e in entries] == [
                ("sub", "sub", True), ("a.txt", "a.txt", False)]
        finally:
            await b.close()

    async def test_ls_subpath(self, fs_daemon):
        b = _onedrive(fs_daemon)
        try:
            await b.ls("sub/deeper")
            assert fs_daemon.calls[-1] == ("ls", "/teams/x/Docs/sub/deeper")
        finally:
            await b.close()

    async def test_get_decodes_base64(self, fs_daemon):
        fs_daemon.get_file = {"filename": "a.txt", "mime": "text/plain",
                              "b64": base64.b64encode(b"hello").decode()}
        b = _onedrive(fs_daemon)
        try:
            name, mime, data = await b.get("a.txt")
            assert fs_daemon.calls[-1] == ("get", "/teams/x/Docs/a.txt")
            assert (name, mime, data) == ("a.txt", "text/plain", b"hello")
        finally:
            await b.close()

    async def test_put_sends_base64_and_abs_path(self, fs_daemon):
        b = _onedrive(fs_daemon)
        try:
            e = await b.put("dir/up.bin", b"\x00\x01\x02", mime="application/octet-stream")
            assert fs_daemon.put_received == (
                "/teams/x/Docs/dir/up.bin", b"\x00\x01\x02", "application/octet-stream")
            assert e.path == "dir/up.bin" and e.size == 3
        finally:
            await b.close()

    async def test_rm_sends_abs_path(self, fs_daemon):
        b = _onedrive(fs_daemon)
        try:
            await b.rm("old.txt")
            assert fs_daemon.rm_path == "/teams/x/Docs/old.txt"
        finally:
            await b.close()

    async def test_search_passes_root(self, fs_daemon):
        fs_daemon.search_entries = [
            {"name": "hit.txt", "path": "/teams/x/Docs/hit.txt", "is_dir": False}]
        b = _onedrive(fs_daemon)
        try:
            hits = await b.search("hit", limit=10)
            assert fs_daemon.search_query["query"] == "hit"
            assert fs_daemon.search_query["root"] == "/teams/x/Docs"
            assert [e.path for e in hits] == ["hit.txt"]
        finally:
            await b.close()


# --------------------------------------------------------------------------- #
# hub bucket handlers                                                          #
# --------------------------------------------------------------------------- #

class _FakeBucket:
    def __init__(self):
        self.calls = []

    async def ls(self, path=""):
        from awm.social.buckets.base import Entry
        self.calls.append(("ls", path))
        return [Entry(name="a.txt", path="a.txt", size=2)]

    async def get(self, path):
        self.calls.append(("get", path))
        return ("a.txt", "text/plain", b"hi")

    async def put(self, path, data, *, mime=None):
        from awm.social.buckets.base import Entry
        self.calls.append(("put", path, data, mime))
        return Entry(name="a.txt", path=path, size=len(data))

    async def rm(self, path):
        self.calls.append(("rm", path))

    async def search(self, query, *, limit=50):
        from awm.social.buckets.base import Entry
        self.calls.append(("search", query, limit))
        return [Entry(name="a.txt", path="a.txt")]


@pytest.fixture()
def wired_buckets(monkeypatch):
    import awm.social.hub_adapter as hub
    from awm.social.config import BucketConfig
    fake = _FakeBucket()
    monkeypatch.setattr(hub, "_buckets", {"b": fake})
    monkeypatch.setattr(hub, "_bucket_cfgs", {
        "b": BucketConfig(name="b", kind="google_drive", root="R")})
    return hub, fake


class TestBucketHandlers:
    def test_h_buckets_lists_config(self, wired_buckets):
        hub, _ = wired_buckets
        out = hub._h_buckets({})
        assert out["buckets"] == [
            {"name": "b", "kind": "google_drive", "root": "R", "live": True}]

    async def test_h_bucket_ls(self, wired_buckets):
        hub, fake = wired_buckets
        out = await hub._h_bucket_ls({"bucket": "b", "path": "sub"})
        assert out["count"] == 1 and out["entries"][0]["name"] == "a.txt"
        assert fake.calls[-1] == ("ls", "sub")

    async def test_h_bucket_get_writes_tempfile(self, wired_buckets):
        import os
        hub, _ = wired_buckets
        out = await hub._h_bucket_get({"bucket": "b", "path": "a.txt"})
        assert out["count"] == 1
        path = out["files"][0]["path"]
        assert os.path.isfile(path)
        with open(path, "rb") as fh:
            assert fh.read() == b"hi"
        os.remove(path)

    async def test_h_bucket_put_reads_src(self, wired_buckets, tmp_path):
        hub, fake = wired_buckets
        src = tmp_path / "up.txt"
        src.write_bytes(b"payload")
        out = await hub._h_bucket_put(
            {"bucket": "b", "path": "dest.txt", "src": str(src)})
        assert out["entry"]["path"] == "dest.txt"
        assert fake.calls[-1] == ("put", "dest.txt", b"payload", None)

    async def test_h_bucket_rm(self, wired_buckets):
        hub, fake = wired_buckets
        out = await hub._h_bucket_rm({"bucket": "b", "path": "gone.txt"})
        assert out["ok"] and fake.calls[-1] == ("rm", "gone.txt")

    async def test_h_bucket_search(self, wired_buckets):
        hub, fake = wired_buckets
        out = await hub._h_bucket_search({"bucket": "b", "query": "x", "limit": 7})
        assert out["count"] == 1 and fake.calls[-1] == ("search", "x", 7)

    async def test_unknown_bucket_raises(self, wired_buckets):
        hub, _ = wired_buckets
        with pytest.raises(RuntimeError):
            await hub._h_bucket_ls({"bucket": "nope"})
