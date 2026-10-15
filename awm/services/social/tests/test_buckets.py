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


def _src(tmp_path, data: bytes, name: str = "src.bin") -> str:
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def _got(info: dict) -> bytes:
    with open(info["path"], "rb") as fh:
        return fh.read()


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
        m["webViewLink"] = f"https://drive.example/{fid}"
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

    def update(self, *, fileId, media_body=None, body=None, fields=None,
               addParents=None, removeParents=None, **kw):
        def run():
            n = self.nodes[fileId]
            if media_body is not None:
                n["content"] = media_body._data
                n["size"] = len(media_body._data)
            if (body or {}).get("trashed"):
                n["parent"] = "trash"
            if (body or {}).get("name"):
                n["name"] = body["name"]
            if addParents:
                assert removeParents == n["parent"]
                n["parent"] = addParents
            return self._meta(fileId)
        return _Req(run)

    def get(self, *, fileId, fields=None, **kw):
        return _Req(lambda: {"id": fileId, "parents": [self.nodes[fileId]["parent"]]})

    def copy(self, *, fileId, body, fields=None, **kw):
        def run():
            self._seq += 1
            fid = f"cp{self._seq}"
            self.nodes[fid] = dict(self.nodes[fileId], name=body["name"],
                                   parent=body["parents"][0])
            return self._meta(fid)
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

    class MediaFileUpload:
        def __init__(self, filename, mimetype=None, resumable=False):
            with open(filename, "rb") as fh:
                self._data = fh.read()
            self.mimetype = mimetype
            self.resumable = resumable

    class MediaIoBaseDownload:
        def __init__(self, buf, request):
            self._buf = buf
            self._data = request.execute()

        def next_chunk(self):
            self._buf.write(self._data)
            return (None, True)

    http.MediaFileUpload = MediaFileUpload
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

    async def test_get_downloads_bytes(self, fake_google_http, tmp_path):
        b = _gdrive(self._tree())
        info = await b.get("docs/note.txt", str(tmp_path))
        assert info["filename"] == "note.txt" and info["mime"] == "text/plain"
        assert _got(info) == b"hello" and info["size"] == 5
        assert info["path"] == str(tmp_path / "note.txt")

    async def test_get_exports_google_doc(self, fake_google_http, tmp_path):
        nodes = self._tree()
        nodes["g1"] = {"name": "plan", "parent": "root", "content": b"DOC",
                       "mimeType": "application/vnd.google-apps.document"}
        b = _gdrive(nodes)
        info = await b.get("plan", str(tmp_path))
        assert info["filename"] == "plan.pdf" and info["mime"] == "application/pdf"
        assert _got(info) == b"EXPORTED:DOC"

    async def test_get_on_folder_raises(self, fake_google_http, tmp_path):
        b = _gdrive(self._tree())
        with pytest.raises(IsADirectoryError):
            await b.get("docs", str(tmp_path))

    async def test_put_creates_new_file(self, fake_google_http, tmp_path):
        b = _gdrive(self._tree())
        e = await b.put("docs/fresh.txt", _src(tmp_path, b"data"), mime="text/plain")
        assert e.name == "fresh.txt" and e.path == "docs/fresh.txt"
        # visible on a re-list
        assert "fresh.txt" in {x.name for x in await b.ls("docs")}

    async def test_put_into_missing_folder_needs_parents(self, fake_google_http, tmp_path):
        b = _gdrive(self._tree())
        src = _src(tmp_path, b"x")
        with pytest.raises(FileNotFoundError):
            await b.put("new/deep/f.txt", src)
        e = await b.put("new/deep/f.txt", src, parents=True)
        assert e.path == "new/deep/f.txt"
        assert "f.txt" in {x.name for x in await b.ls("new/deep")}

    async def test_put_overwrites_existing(self, fake_google_http, tmp_path):
        b = _gdrive(self._tree())
        e = await b.put("docs/note.txt", _src(tmp_path, b"replaced"))
        assert e.id == "f1"          # same node updated, not a new one
        out = tmp_path / "out"
        out.mkdir()
        assert _got(await b.get("docs/note.txt", str(out))) == b"replaced"

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

    def _where(self, b, fid):
        n = b._svc.files().nodes[fid]
        return n["parent"], n["name"]

    async def test_entries_carry_web_view_link(self):
        b = _gdrive(self._tree())
        assert (await b.stat("pic.png")).web_url == "https://drive.example/f2"

    async def test_mv_renames_and_moves(self):
        b = _gdrive(self._tree())
        e = await b.mv("pic.png", "docs/p2.png")
        assert e.path == "docs/p2.png" and e.id == "f2"
        assert self._where(b, "f2") == ("d1", "p2.png")

    async def test_mv_onto_existing_needs_overwrite(self):
        b = _gdrive(self._tree())
        with pytest.raises(FileExistsError):
            await b.mv("pic.png", "docs/note.txt")
        await b.mv("pic.png", "docs/note.txt", overwrite=True)
        assert self._where(b, "f1")[0] == "trash"
        assert self._where(b, "f2") == ("d1", "note.txt")

    async def test_mv_missing_parent_needs_parents(self):
        b = _gdrive(self._tree())
        with pytest.raises(FileNotFoundError):
            await b.mv("pic.png", "new/pic.png")
        e = await b.mv("pic.png", "new/pic.png", parents=True)
        assert e.path == "new/pic.png"

    async def test_cp_folder_rebuilds_tree(self):
        b = _gdrive(self._tree())
        e = await b.cp("docs", "docs2")
        assert e.is_dir and e.id != "d1"
        assert [x.name for x in await b.ls("docs2")] == ["note.txt"]
        assert [x.name for x in await b.ls("docs")] == ["note.txt"]

    async def test_mv_into_itself_is_refused(self):
        b = _gdrive(self._tree())
        with pytest.raises(ValueError):
            await b.mv("docs", "docs/inner")

    async def test_search_name_contains(self):
        b = _gdrive(self._tree())
        hits = {e.name for e in await b.search("note")}
        assert hits == {"note.txt"}


# --------------------------------------------------------------------------- #
# OneDrive (stub mira daemon)                                                  #
# --------------------------------------------------------------------------- #

class TestTrees:
    """walk / get_tree / put_tree live in the Bucket base; Drive exercises them."""

    def _tree(self):
        return {
            "d1": {"name": "docs", "mimeType": _F, "parent": "root"},
            "d2": {"name": "deep dir", "mimeType": _F, "parent": "d1"},
            "f1": {"name": "note.txt", "mimeType": "text/plain", "parent": "d1",
                   "size": 5, "content": b"hello"},
            "f2": {"name": "a b.txt", "mimeType": "text/plain", "parent": "d2",
                   "size": 2, "content": b"ab"},
            "f3": {"name": "pic.png", "mimeType": "image/png", "parent": "root",
                   "size": 3, "content": b"img"},
        }

    async def test_walk_is_breadth_first_and_bounded(self):
        b = _gdrive(self._tree())
        entries, truncated = await b.walk("")
        assert [e.path for e in entries] == [
            "docs", "pic.png", "docs/deep dir", "docs/note.txt",
            "docs/deep dir/a b.txt"]
        assert truncated is False
        entries, truncated = await b.walk("", limit=2)
        assert len(entries) == 2 and truncated is True

    async def test_get_tree_writes_a_faithful_tree(self, fake_google_http, tmp_path):
        b = _gdrive(self._tree())
        out = await b.get_tree("docs", str(tmp_path))
        root = tmp_path / "docs"
        assert out["path"] == str(root) and out["is_dir"] is True
        assert out["count"] == 2 and out["size"] == 7 and out["failures"] == []
        assert (root / "note.txt").read_bytes() == b"hello"
        assert (root / "deep dir" / "a b.txt").read_bytes() == b"ab"

    async def test_get_tree_reports_failures_and_continues(
            self, fake_google_http, tmp_path, monkeypatch):
        b = _gdrive(self._tree())
        real_get = b.get

        async def flaky(path, dest_dir):
            if path.endswith("note.txt"):
                raise PermissionError("denied")
            return await real_get(path, dest_dir)
        monkeypatch.setattr(b, "get", flaky)
        out = await b.get_tree("docs", str(tmp_path))
        assert out["count"] == 1
        assert out["failures"] == [
            {"path": "docs/note.txt", "error": "PermissionError: denied"}]

    async def test_get_tree_refuses_past_limit(self, fake_google_http, tmp_path):
        b = _gdrive(self._tree())
        with pytest.raises(ValueError, match="larger limit"):
            await b.get_tree("docs", str(tmp_path), limit=1)

    async def test_put_tree_uploads_a_tree(self, fake_google_http, tmp_path):
        b = _gdrive(self._tree())
        (tmp_path / "up" / "sub").mkdir(parents=True)
        (tmp_path / "up" / "top.txt").write_bytes(b"t")
        (tmp_path / "up" / "sub" / "low.txt").write_bytes(b"l")
        out = await b.put_tree("docs/up", str(tmp_path / "up"))
        assert out["count"] == 2 and out["failures"] == []
        assert out["entry"].is_dir
        assert [e.path for e in (await b.walk("docs/up"))[0]] == [
            "docs/up/sub", "docs/up/top.txt", "docs/up/sub/low.txt"]

    async def test_put_tree_missing_parent_needs_parents(self, fake_google_http, tmp_path):
        b = _gdrive(self._tree())
        (tmp_path / "up").mkdir()
        with pytest.raises(FileNotFoundError):
            await b.put_tree("nope/up", str(tmp_path / "up"))
        out = await b.put_tree("nope/up", str(tmp_path / "up"), parents=True)
        assert out["entry"].path == "nope/up"

    async def test_hub_moves_folders_both_ways(self, fake_google_http, tmp_path, monkeypatch):
        import shutil
        import awm.social.hub_adapter as hub
        b = _gdrive(self._tree())
        monkeypatch.setattr(hub, "_buckets", {"g": b})
        out = await hub._h_bucket_get({"bucket": "g", "path": "docs"})
        f = out["files"][0]
        assert f["is_dir"] and f["count"] == 2 and "failures" not in out
        put = await hub._h_bucket_put({"bucket": "g", "path": "copy", "src": f["path"]})
        assert put["count"] == 2 and put["entry"]["is_dir"]
        shutil.rmtree(out["dir"])
        ls = await hub._h_bucket_ls({"bucket": "g", "path": "copy", "recursive": True})
        assert ls["count"] == 3 and ls["truncated"] is False


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
        self.mkdir_path = None
        self.chunks = []          # upload bodies, in order
        self.cancelled = []       # upload ids
        self.fail_upload_at = None  # offset whose chunk answers 502
        self.etag_flip_at = None    # offset from which get reports a new etag
        self.fail = None          # (status, code, message) for the next call
        self._runner = None
        self.base = ""

    async def start(self):
        from aiohttp import web
        app = web.Application()
        app.router.add_get("/v1/fs/{name}/ls", self._ls)
        app.router.add_get("/v1/fs/{name}/stat", self._stat)
        app.router.add_post("/v1/fs/{name}/mkdir", self._mkdir)
        app.router.add_get("/v1/fs/{name}/get", self._get)
        app.router.add_get("/v1/fs/{name}/search", self._search)
        app.router.add_get("/v1/fs/{name}/sites", self._sites)
        app.router.add_post("/v1/fs/{name}/share", self._echo)
        app.router.add_post("/v1/fs/{name}/unshare", self._echo)
        app.router.add_get("/v1/fs/{name}/shares", self._echo_query)
        app.router.add_get("/v1/fs/{name}/resolve", self._resolve)
        app.router.add_post("/v1/fs/{name}/put", self._put)
        app.router.add_post("/v1/fs/{name}/upload", self._upload)
        app.router.add_post("/v1/fs/{name}/rm", self._rm)
        app.router.add_post("/v1/fs/{name}/mv", self._transfer)
        app.router.add_post("/v1/fs/{name}/cp", self._transfer)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{list(self._runner.addresses)[0][1]}"

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()

    def _failure(self):
        from aiohttp import web
        if self.fail is None:
            return None
        status, code, msg = self.fail
        self.fail = None
        return web.json_response({"error": msg, "code": code}, status=status)

    async def _ls(self, request):
        from aiohttp import web
        self.calls.append(("ls", request.query.get("path")))
        return self._failure() or web.json_response({"entries": self.ls_entries})

    async def _stat(self, request):
        from aiohttp import web
        path = request.query.get("path")
        self.calls.append(("stat", path))
        return self._failure() or web.json_response({"entry": {
            "name": path.rsplit("/", 1)[-1], "path": path, "is_dir": True}})

    async def _mkdir(self, request):
        from aiohttp import web
        body = await request.json()
        self.mkdir_path = body["path"]
        return self._failure() or web.json_response({"entry": {
            "name": body["path"].rsplit("/", 1)[-1], "path": body["path"],
            "is_dir": True}})

    async def _get(self, request):
        from aiohttp import web
        q = request.query
        self.calls.append(("get", q.get("path"), int(q["offset"]), int(q["length"])))
        failure = self._failure()
        if failure:
            return failure
        data = self.get_file["data"]
        offset, length = int(q["offset"]), int(q["length"])
        flipped = self.etag_flip_at is not None and offset >= self.etag_flip_at
        return web.json_response({
            "filename": self.get_file["filename"], "mime": self.get_file["mime"],
            "size": len(data), "offset": offset,
            "etag": "v2" if flipped else "v1",
            "b64": base64.b64encode(data[offset:offset + length]).decode()})

    async def _upload(self, request):
        from aiohttp import web
        body = await request.json()
        if body.get("cancel"):
            self.cancelled.append(body["upload_id"])
            return web.json_response({"ok": True})
        self.chunks.append(body)
        if body["offset"] == self.fail_upload_at:
            return web.json_response({"error": "boom", "code": "EUPSTREAM"}, status=502)
        n = body["offset"] + len(base64.b64decode(body["b64"]))
        if body["final"]:
            return web.json_response({"entry": {
                "name": body["path"].rsplit("/", 1)[-1], "path": body["path"],
                "is_dir": False, "size": n}})
        return web.json_response({"offset": n})

    async def _echo(self, request):
        from aiohttp import web
        body = await request.json()
        self.calls.append((request.path.rsplit("/", 1)[-1], body))
        return self._failure() or web.json_response({"ok": True, **body})

    async def _echo_query(self, request):
        from aiohttp import web
        self.calls.append((request.path.rsplit("/", 1)[-1], dict(request.query)))
        return web.json_response({"links": [], "people": []})

    async def _resolve(self, request):
        from aiohttp import web
        self.calls.append(("resolve", request.query.get("url")))
        return web.json_response({"via": "token", "entry": {
            "name": "Engcyc", "path": "/teams/x/Docs/Outputs/Engcyc", "is_dir": True}})

    async def _sites(self, request):
        from aiohttp import web
        self.calls.append(("sites", request.query.get("site")))
        return web.json_response({"entries": [
            {"name": "X", "path": "/teams/x", "kind": "site",
             "web_url": "https://t/teams/x"}]})

    async def _search(self, request):
        from aiohttp import web
        self.search_query = dict(request.query)
        return web.json_response({"entries": self.search_entries})

    async def _put(self, request):
        from aiohttp import web
        body = await request.json()
        self.put_received = (
            body["path"], base64.b64decode(body["b64"]), body.get("mime"))
        self.put_parents = body.get("parents")
        return web.json_response({"entry": {
            "name": body["path"].rsplit("/", 1)[-1], "path": body["path"],
            "is_dir": False, "size": len(base64.b64decode(body["b64"]))}})

    async def _transfer(self, request):
        from aiohttp import web
        body = await request.json()
        self.calls.append((request.path.rsplit("/", 1)[-1], body))
        return self._failure() or web.json_response({"entry": {
            "name": body["dst"].rsplit("/", 1)[-1], "path": body["dst"],
            "is_dir": False}})

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

    async def test_get_writes_file(self, fs_daemon, tmp_path):
        fs_daemon.get_file = {"filename": "a.txt", "mime": "text/plain", "data": b"hello"}
        b = _onedrive(fs_daemon)
        try:
            info = await b.get("a.txt", str(tmp_path))
            assert fs_daemon.calls[-1][:2] == ("get", "/teams/x/Docs/a.txt")
            assert (info["filename"], info["mime"], info["size"]) == ("a.txt", "text/plain", 5)
            assert _got(info) == b"hello"
        finally:
            await b.close()

    async def test_get_reads_in_chunks(self, fs_daemon, tmp_path, monkeypatch):
        import awm.social.buckets.onedrive_bucket as od
        monkeypatch.setattr(od, "CHUNK", 4)
        fs_daemon.get_file = {"filename": "b.bin", "mime": "", "data": b"0123456789"}
        b = _onedrive(fs_daemon)
        try:
            info = await b.get("b.bin", str(tmp_path))
            assert _got(info) == b"0123456789" and info["size"] == 10
            assert [c[2:] for c in fs_daemon.calls] == [(0, 4), (4, 4), (8, 4)]
        finally:
            await b.close()

    async def test_get_empty_file(self, fs_daemon, tmp_path):
        fs_daemon.get_file = {"filename": "e", "mime": "", "data": b""}
        b = _onedrive(fs_daemon)
        try:
            info = await b.get("e", str(tmp_path))
            assert info["size"] == 0 and _got(info) == b""
        finally:
            await b.close()

    async def test_get_aborts_when_file_changes(self, fs_daemon, tmp_path, monkeypatch):
        import awm.social.buckets.onedrive_bucket as od
        monkeypatch.setattr(od, "CHUNK", 4)
        fs_daemon.get_file = {"filename": "b.bin", "mime": "", "data": b"0123456789"}
        fs_daemon.etag_flip_at = 4
        b = _onedrive(fs_daemon)
        try:
            with pytest.raises(RuntimeError, match="changed during download"):
                await b.get("b.bin", str(tmp_path))
            assert list(tmp_path.iterdir()) == []  # partial file removed
        finally:
            await b.close()

    async def test_get_missing_leaves_nothing(self, fs_daemon, tmp_path):
        fs_daemon.fail = (404, "ENOENT", "/teams/x/Docs/nope: not found")
        b = _onedrive(fs_daemon)
        try:
            with pytest.raises(FileNotFoundError):
                await b.get("nope", str(tmp_path))
            assert list(tmp_path.iterdir()) == []
        finally:
            await b.close()

    async def test_put_large_file_uploads_in_chunks(self, fs_daemon, tmp_path, monkeypatch):
        import awm.social.buckets.onedrive_bucket as od
        monkeypatch.setattr(od, "CHUNK", 4)
        b = _onedrive(fs_daemon)
        try:
            e = await b.put("big.bin", _src(tmp_path, b"0123456789"), parents=True)
            assert e.path == "big.bin" and e.size == 10
            assert [(c["offset"], c["final"]) for c in fs_daemon.chunks] == [
                (0, False), (4, False), (8, True)]
            assert len({c["upload_id"] for c in fs_daemon.chunks}) == 1
            assert all(c["path"] == "/teams/x/Docs/big.bin" and c["parents"]
                       for c in fs_daemon.chunks)
            assert b"".join(base64.b64decode(c["b64"]) for c in fs_daemon.chunks) == b"0123456789"
            assert fs_daemon.put_received is None  # never the one-shot route
        finally:
            await b.close()

    async def test_put_failed_chunk_cancels_session(self, fs_daemon, tmp_path, monkeypatch):
        import awm.social.buckets.onedrive_bucket as od
        monkeypatch.setattr(od, "CHUNK", 4)
        fs_daemon.fail_upload_at = 4
        b = _onedrive(fs_daemon)
        try:
            with pytest.raises(RuntimeError, match="boom"):
                await b.put("big.bin", _src(tmp_path, b"0123456789"))
            assert fs_daemon.cancelled == [fs_daemon.chunks[0]["upload_id"]]
        finally:
            await b.close()

    async def test_put_sends_base64_and_abs_path(self, fs_daemon, tmp_path):
        b = _onedrive(fs_daemon)
        try:
            e = await b.put("dir/up.bin", _src(tmp_path, b"\x00\x01\x02"),
                            mime="application/octet-stream")
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

    async def test_put_threads_parents(self, fs_daemon, tmp_path):
        b = _onedrive(fs_daemon)
        src = _src(tmp_path, b"x")
        try:
            await b.put("a/b.bin", src, parents=True)
            assert fs_daemon.put_parents is True
            await b.put("a/b.bin", src)
            assert fs_daemon.put_parents is False
        finally:
            await b.close()

    async def test_mkdir_sends_abs_path(self, fs_daemon):
        b = _onedrive(fs_daemon)
        try:
            e = await b.mkdir("new/dir")
            assert fs_daemon.mkdir_path == "/teams/x/Docs/new/dir"
            assert e.path == "new/dir" and e.is_dir
        finally:
            await b.close()

    @pytest.mark.parametrize("status,code,exc", [
        (404, "ENOENT", FileNotFoundError),
        (409, "ENOTDIR", NotADirectoryError),
        (403, "EAUTH", PermissionError),
        (400, "EINVAL", ValueError),
        (502, "EUPSTREAM", RuntimeError),
    ])
    async def test_coded_errors_map_to_builtins(self, fs_daemon, status, code, exc):
        fs_daemon.fail = (status, code, "/teams/x/Docs/nope: not found")
        b = _onedrive(fs_daemon)
        try:
            with pytest.raises(exc) as info:
                await b.ls("nope")
            if code != "EUPSTREAM":
                assert "bucket 'o'" in str(info.value)
            if code == "EAUTH":
                assert "re-authenticate" in str(info.value)
        finally:
            await b.close()

    async def test_absolute_paths_pass_through(self, fs_daemon):
        b = _onedrive(fs_daemon, root="/teams/x/Docs")
        try:
            await b.ls("/teams/x-Compute2/Shared Documents/Outputs")
            assert fs_daemon.calls[-1] == (
                "ls", "/teams/x-Compute2/Shared Documents/Outputs")
            await b.ls("/personal/me_ubc_ca/Documents/")
            assert fs_daemon.calls[-1] == ("ls", "/personal/me_ubc_ca/Documents")
            # A leading slash on a non-managed path stays relative to the root.
            await b.ls("/Outputs")
            assert fs_daemon.calls[-1] == ("ls", "/teams/x/Docs/Outputs")
        finally:
            await b.close()

    async def test_entries_strip_root_by_whole_segment(self, fs_daemon):
        fs_daemon.ls_entries = [
            {"name": "a", "path": "/teams/x/Docs/a", "is_dir": True},
            {"name": "b", "path": "/teams/x/Docs2/b", "is_dir": True},
            {"name": "c", "path": "/teams/y/Docs/c", "is_dir": True},
        ]
        b = _onedrive(fs_daemon, root="/teams/x/Docs")
        try:
            got = [e.path for e in await b.ls("")]
            assert got == ["a", "/teams/x/Docs2/b", "/teams/y/Docs/c"]
        finally:
            await b.close()

    async def test_mv_and_cp_send_abs_paths(self, fs_daemon):
        b = _onedrive(fs_daemon)
        e = await b.mv("a.txt", "sub/b.txt", overwrite=True)
        assert e.path == "sub/b.txt"
        assert fs_daemon.calls[-1] == ("mv", {
            "src": "/teams/x/Docs/a.txt", "dst": "/teams/x/Docs/sub/b.txt",
            "overwrite": True, "parents": False})
        e = await b.cp("a.txt", "/teams/y/Docs/a.txt", parents=True)
        assert e.path == "/teams/y/Docs/a.txt"
        assert fs_daemon.calls[-1][0] == "cp"
        assert fs_daemon.calls[-1][1]["parents"] is True
        await b.close()

    async def test_mv_existing_target_is_file_exists(self, fs_daemon):
        b = _onedrive(fs_daemon)
        fs_daemon.fail = (409, "EEXIST", "/teams/x/Docs/b exists")
        with pytest.raises(FileExistsError, match="'o'"):
            await b.mv("a", "b")
        await b.close()

    async def test_sites_threads_site(self, fs_daemon):
        b = _onedrive(fs_daemon)
        rows = await b.sites()
        assert rows[0]["kind"] == "site" and fs_daemon.calls[-1] == ("sites", None)
        await b.sites("teams/x/")
        assert fs_daemon.calls[-1] == ("sites", "/teams/x")
        await b.close()

    async def test_entries_carry_web_url(self, fs_daemon):
        b = _onedrive(fs_daemon)
        fs_daemon.ls_entries = [{"name": "a", "path": "/teams/x/Docs/a",
                                 "web_url": "https://t/a?web=1"}]
        assert (await b.ls(""))[0].web_url == "https://t/a?web=1"
        await b.close()

    async def test_sharing_sends_abs_paths(self, fs_daemon):
        b = _onedrive(fs_daemon)
        out = await b.share("s", scope="people", role="edit", people=["a@x"])
        assert out["path"] == "/teams/x/Docs/s" and out["send_email"] is False
        await b.shares("s")
        assert fs_daemon.calls[-1] == ("shares", {"path": "/teams/x/Docs/s"})
        await b.unshare("s", person="a@x")
        assert fs_daemon.calls[-1] == ("unshare", {"path": "/teams/x/Docs/s", "person": "a@x"})
        entry, via = await b.resolve("https://t/:f:/t/x/tok")
        assert (entry.path, entry.is_dir, via) == ("Outputs/Engcyc", True, "token")
        await b.close()

    async def test_probe_stats_root(self, fs_daemon):
        b = _onedrive(fs_daemon)
        try:
            await b.probe()
            assert fs_daemon.calls[-1] == ("stat", "/teams/x/Docs")
            fs_daemon.fail = (403, "EAUTH", "signed out")
            with pytest.raises(PermissionError):
                await b.probe()
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
        self.dead = False

    async def ls(self, path=""):
        from awm.social.buckets.base import Entry
        self.calls.append(("ls", path))
        return [Entry(name="a.txt", path="a.txt", size=2)]

    async def get(self, path, dest_dir):
        import os
        self.calls.append(("get", path))
        if path == "boom":
            raise FileNotFoundError(path)
        local = os.path.join(dest_dir, "a.txt")
        with open(local, "wb") as fh:
            fh.write(b"hi")
        return {"filename": "a.txt", "mime": "text/plain", "size": 2, "path": local}

    async def put(self, path, src, *, mime=None, parents=False):
        from awm.social.buckets.base import Entry
        with open(src, "rb") as fh:
            data = fh.read()
        self.calls.append(("put", path, data, mime, parents))
        return Entry(name="a.txt", path=path, size=len(data))

    async def stat(self, path):
        from awm.social.buckets.base import Entry
        self.calls.append(("stat", path))
        if self.dead:
            raise PermissionError("session expired")
        return Entry(name="", path=path, is_dir=True)

    async def probe(self):
        await self.stat("")

    async def mkdir(self, path):
        from awm.social.buckets.base import Entry
        self.calls.append(("mkdir", path))
        return Entry(name=path.rsplit("/", 1)[-1], path=path, is_dir=True)

    async def rm(self, path):
        self.calls.append(("rm", path))

    async def mv(self, src, dst, *, overwrite=False, parents=False):
        from awm.social.buckets.base import Entry
        self.calls.append(("mv", src, dst, overwrite, parents))
        return Entry(name=dst.rsplit("/", 1)[-1], path=dst)

    async def cp(self, src, dst, *, overwrite=False, parents=False):
        from awm.social.buckets.base import Entry
        self.calls.append(("cp", src, dst, overwrite, parents))
        return Entry(name=dst.rsplit("/", 1)[-1], path=dst)

    async def share(self, path, *, scope, role, people=None, send_email=False):
        self.calls.append(("share", path, scope, role, people, send_email))
        return {"url": "https://t/x", "scope": scope, "role": role}

    async def unshare(self, path, *, link="", person=""):
        self.calls.append(("unshare", path, link, person))
        return {"ok": True}

    async def search(self, query, *, limit=50, path=""):
        from awm.social.buckets.base import Entry
        self.calls.append(("search", query, limit, path))
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
    async def test_h_buckets_lists_config(self, wired_buckets):
        hub, _ = wired_buckets
        out = await hub._h_buckets({})
        assert out["buckets"] == [
            {"name": "b", "kind": "google_drive", "root": "R", "live": True}]

    async def test_h_buckets_reports_dead_session(self, wired_buckets):
        hub, fake = wired_buckets
        fake.dead = True
        row = (await hub._h_buckets({}))["buckets"][0]
        assert row["live"] is False
        assert "PermissionError" in row["error"]

    async def test_h_bucket_stat_and_mkdir(self, wired_buckets):
        hub, fake = wired_buckets
        out = await hub._h_bucket_mkdir({"bucket": "b", "path": "a/b"})
        assert out["entry"]["is_dir"] and fake.calls[-1] == ("mkdir", "a/b")
        out = await hub._h_bucket_stat({"bucket": "b", "path": "a/b"})
        assert out["entry"]["path"] == "a/b"

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
        assert out["dir"] == os.path.dirname(path)
        os.remove(path)
        os.rmdir(out["dir"])

    async def test_h_bucket_get_failure_removes_temp_dir(self, wired_buckets, monkeypatch):
        import tempfile
        hub, _ = wired_buckets
        made = []
        monkeypatch.setattr(hub, "sink_dir", lambda: made.append(tempfile.mkdtemp()) or made[-1])
        with pytest.raises(FileNotFoundError):
            await hub._h_bucket_get({"bucket": "b", "path": "boom"})
        import os
        assert made and not os.path.exists(made[0])

    async def test_h_bucket_put_missing_src(self, wired_buckets, tmp_path):
        hub, _ = wired_buckets
        with pytest.raises(FileNotFoundError, match="no local file"):
            await hub._h_bucket_put(
                {"bucket": "b", "path": "x", "src": str(tmp_path / "nope")})

    async def test_h_bucket_put_reads_src(self, wired_buckets, tmp_path):
        hub, fake = wired_buckets
        src = tmp_path / "up.txt"
        src.write_bytes(b"payload")
        out = await hub._h_bucket_put(
            {"bucket": "b", "path": "dest.txt", "src": str(src)})
        assert out["entry"]["path"] == "dest.txt"
        assert fake.calls[-1] == ("put", "dest.txt", b"payload", None, False)
        await hub._h_bucket_put(
            {"bucket": "b", "path": "d/e.txt", "src": str(src), "parents": True})
        assert fake.calls[-1][-1] is True

    async def test_h_bucket_rm(self, wired_buckets):
        hub, fake = wired_buckets
        out = await hub._h_bucket_rm({"bucket": "b", "path": "gone.txt"})
        assert out["ok"] and fake.calls[-1] == ("rm", "gone.txt")

    async def test_h_bucket_search(self, wired_buckets):
        hub, fake = wired_buckets
        out = await hub._h_bucket_search({"bucket": "b", "query": "x", "limit": 7})
        assert out["count"] == 1 and fake.calls[-1] == ("search", "x", 7, "")

    async def test_h_bucket_mv_and_cp(self, wired_buckets):
        hub, fake = wired_buckets
        out = await hub._h_bucket_mv(
            {"bucket": "b", "src": "a", "dst": "c/d", "overwrite": True})
        assert out["entry"]["path"] == "c/d"
        assert fake.calls[-1] == ("mv", "a", "c/d", True, False)
        await hub._h_bucket_cp(
            {"bucket": "b", "src": "a", "dst": "e", "parents": True})
        assert fake.calls[-1] == ("cp", "a", "e", False, True)

    async def test_h_bucket_share_defaults_to_an_org_view_link(self, wired_buckets):
        hub, fake = wired_buckets
        await hub._h_bucket_share({"bucket": "b", "path": "s"})
        assert fake.calls[-1] == ("share", "s", "organization", "view", [], False)
        await hub._h_bucket_share({"bucket": "b", "path": "s", "scope": "people",
                                   "people": "a@x, b@y"})
        assert fake.calls[-1][4] == ["a@x", "b@y"]

    async def test_h_bucket_unshare_takes_exactly_one_target(self, wired_buckets):
        hub, fake = wired_buckets
        with pytest.raises(ValueError):
            await hub._h_bucket_unshare({"bucket": "b", "path": "s"})
        with pytest.raises(ValueError):
            await hub._h_bucket_unshare(
                {"bucket": "b", "path": "s", "link": "l", "person": "p"})
        await hub._h_bucket_unshare({"bucket": "b", "path": "s", "link": "l"})
        assert fake.calls[-1] == ("unshare", "s", "l", "")

    async def test_unknown_bucket_raises(self, wired_buckets):
        hub, _ = wired_buckets
        with pytest.raises(RuntimeError):
            await hub._h_bucket_ls({"bucket": "nope"})
