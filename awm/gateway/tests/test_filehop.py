"""The file hop: local files riding one peer ``/invoke`` call, both directions.

The codec is tested directly, the gateway's ``/invoke`` through an ASGI client
with dispatch stubbed, and the MCP proxy end to end by pointing its peer client
at the gateway app in-process — the edge in between only streams bytes.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import tempfile

import httpx
import pytest

from awm.gateway import filehop

pytestmark = [pytest.mark.hub]


def _tree(root) -> dict[str, bytes]:
    out = {}
    for top, _dirs, names in os.walk(root):
        for n in names:
            p = os.path.join(top, n)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = fh.read()
    return out


def _domain_call(**inner):
    return {"name": "social", "args": {"verb": "bucket_put", "args": inner}}


# -- codec -------------------------------------------------------------------

class TestCodec:
    def test_spec_from_schema(self):
        schema = {"properties": {"src": {"type": "string", filehop.FILE_KEY: "in"},
                                 "path": {"type": "string"}},
                  filehop.RETURNS_KEY: True, filehop.TIMEOUT_KEY: 3600.0}
        assert filehop.spec_from_schema(schema) == {
            "file_params": ["src"], "returns_files": True, "timeout": 3600.0}
        assert filehop.spec_from_schema(None)["file_params"] == []

    def test_local_files_takes_only_existing_paths(self, tmp_path):
        f = tmp_path / "a.bin"
        f.write_bytes(b"x")
        call = _domain_call(src=str(f), other=str(f))
        assert filehop.local_files(call, ["src"]) == {"src": str(f)}
        call = _domain_call(src=str(tmp_path / "nope"))
        assert filehop.local_files(call, ["src"]) == {}

    def test_call_round_trip_with_file_and_folder(self, tmp_path):
        f = tmp_path / "big.bin"
        f.write_bytes(os.urandom(70_000))
        d = tmp_path / "tree"
        (d / "sub").mkdir(parents=True)
        (d / "a.txt").write_bytes(b"A")
        (d / "sub" / "b.txt").write_bytes(b"B")
        call = _domain_call(src=str(f), dir=str(d), path="x/big.bin")
        packed = filehop.pack_call(call, {"src": str(f), "dir": str(d)})
        body = tmp_path / "body.tar"
        body.write_bytes(packed.read())
        spool = tmp_path / "spool"
        spool.mkdir()
        out = filehop.unpack_call(str(body), str(spool))
        inner = out["args"]["args"]
        assert inner["path"] == "x/big.bin"
        assert inner["src"].startswith(str(spool) + os.sep)
        with open(inner["src"], "rb") as fh:
            assert fh.read() == f.read_bytes()
        assert _tree(inner["dir"]) == {"a.txt": b"A", os.path.join("sub", "b.txt"): b"B"}
        assert "files" not in out
        assert call["args"]["args"]["src"] == str(f)  # caller's dict untouched

    def test_unpack_refuses_escaping_members(self, tmp_path):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            data = json.dumps({"name": "x", "args": {}, "files": {}}).encode()
            info = tarfile.TarInfo("call.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            evil = tarfile.TarInfo("files/src/../../../escape.txt")
            evil.size = 1
            tar.addfile(evil, io.BytesIO(b"!"))
        body = tmp_path / "body.tar"
        body.write_bytes(buf.getvalue())
        spool = tmp_path / "spool"
        spool.mkdir()
        with pytest.raises(tarfile.TarError):
            filehop.unpack_call(str(body), str(spool))
        assert not (tmp_path / "escape.txt").exists()

    def test_result_round_trip(self, tmp_path):
        src = tmp_path / "far"
        src.mkdir()
        a = src / "a.txt"
        a.write_bytes(b"hello" * 300)
        folder = src / "dl"
        (folder / "x").mkdir(parents=True)
        (folder / "x" / "deep.bin").write_bytes(b"\x00" * 1000)
        result = json.dumps({"files": [{"path": str(a), "size": 1500},
                                       {"path": str(folder)}],
                             "count": 2, "dir": str(src)})
        tar_path = tmp_path / "r.tar"
        tar_path.write_bytes(b"".join(filehop.iter_result(result, filehop.result_paths(result))))
        dest = tmp_path / "local"
        dest.mkdir()
        out = json.loads(filehop.unpack_result(str(tar_path), str(dest)))
        p0, p1 = (f["path"] for f in out["files"])
        assert p0.startswith(str(dest)) and open(p0, "rb").read() == b"hello" * 300
        assert _tree(p1) == {os.path.join("x", "deep.bin"): b"\x00" * 1000}
        assert out["dir"] == str(dest) and out["count"] == 2

    def test_single_file_result_dir_is_its_folder(self, tmp_path):
        a = tmp_path / "a.txt"
        a.write_bytes(b"x")
        result = json.dumps({"files": [{"path": str(a)}], "dir": str(tmp_path)})
        tar_path = tmp_path / "r.tar"
        tar_path.write_bytes(b"".join(filehop.iter_result(result, [str(a)])))
        dest = tmp_path / "local"
        dest.mkdir()
        out = json.loads(filehop.unpack_result(str(tar_path), str(dest)))
        assert out["dir"] == os.path.dirname(out["files"][0]["path"])

    def test_remove_sent_stays_inside_tempdir(self, tmp_path):
        d = tempfile.mkdtemp(prefix="awm-social-")
        f = os.path.join(d, "a.txt")
        open(f, "w").close()
        filehop.remove_sent([f])
        assert not os.path.exists(d)
        keep = tmp_path / "keep.txt"
        keep.write_text("x")
        if not str(keep.resolve()).startswith(os.path.realpath(tempfile.gettempdir())):
            filehop.remove_sent([str(keep)])
            assert keep.exists()


class TestCatalogProjection:
    def test_manifest_markers_reach_the_schema(self):
        from awm.gateway.catalog import _fn_to_tool
        from awm.gateway.hub.registry import ServiceRecord

        rec = ServiceRecord.__new__(ServiceRecord)
        rec.name = "social"
        tool = _fn_to_tool(rec, {
            "name": "bucket_put", "tool": "social_bucket_put", "timeout": 3600,
            "returns_files": True,
            "params": [{"name": "src", "type": "string", "file": "in"},
                       {"name": "path", "type": "string"}]})
        spec = filehop.spec_from_schema(tool.inputSchema)
        assert spec == {"file_params": ["src"], "returns_files": True, "timeout": 3600.0}


# -- gateway /invoke ---------------------------------------------------------

@pytest.fixture()
def gateway(monkeypatch, awm_workspace):
    """The gateway app with dispatch stubbed: ``seen`` records each call, and
    the handler reads file args while the spool still exists."""
    from awm.gateway import catalog, server

    seen: list = []
    spec = {"file_params": ["src"], "returns_files": True, "timeout": 30.0}
    state = {"result_files": []}

    async def dispatch(name, args, as_=None):
        inner = args.get("args", {})
        src = inner.get("src")
        content = open(src, "rb").read() if src and os.path.isfile(src) else None
        seen.append({"name": name, "args": args, "content": content})
        return json.dumps({"files": [{"path": p} for p in state["result_files"]],
                           "count": len(state["result_files"])})

    monkeypatch.setattr(catalog, "dispatch", dispatch)
    monkeypatch.setattr(catalog, "call_spec", lambda name, args: spec)
    return server.app, seen, state, spec


def _asgi(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")


class TestInvoke:
    async def test_json_call_unchanged(self, gateway):
        app, seen, _state, _spec = gateway
        async with _asgi(app) as c:
            r = await c.post("/invoke", json=_domain_call(path="p"))
        assert r.status_code == 200 and json.loads(r.json()["result"])["count"] == 0
        assert seen[-1]["args"]["args"] == {"path": "p"}

    async def test_non_object_body_is_422(self, gateway):
        app, *_ = gateway
        async with _asgi(app) as c:
            r = await c.post("/invoke", json=[1, 2])
        assert r.status_code == 422

    async def test_tar_call_spools_files_and_cleans_up(self, gateway, tmp_path):
        app, seen, _state, _spec = gateway
        f = tmp_path / "up.bin"
        f.write_bytes(b"payload" * 1000)
        body = filehop.pack_call(_domain_call(src=str(f), path="d/up.bin"),
                                 {"src": str(f)}).read()
        async with _asgi(app) as c:
            r = await c.post("/invoke", content=body,
                             headers={"Content-Type": filehop.CONTENT_TYPE})
        assert r.status_code == 200
        call = seen[-1]
        assert call["content"] == b"payload" * 1000
        spooled = call["args"]["args"]["src"]
        assert spooled != str(f) and not os.path.exists(spooled)

    async def test_garbage_tar_is_400(self, gateway):
        app, *_ = gateway
        async with _asgi(app) as c:
            r = await c.post("/invoke", content=b"not a tar",
                             headers={"Content-Type": filehop.CONTENT_TYPE})
        assert r.status_code == 400

    async def test_return_files_streams_tar_and_removes_originals(self, gateway, tmp_path):
        app, _seen, state, _spec = gateway
        d = tempfile.mkdtemp(prefix="awm-social-")
        f = os.path.join(d, "got.bin")
        with open(f, "wb") as fh:
            fh.write(b"result-bytes")
        state["result_files"] = [f]
        async with _asgi(app) as c:
            r = await c.post("/invoke", json=_domain_call(path="p"),
                             headers={filehop.RETURN_FILES_HEADER: "1"})
        assert r.headers["content-type"].startswith(filehop.CONTENT_TYPE)
        tar_path = tmp_path / "r.tar"
        tar_path.write_bytes(r.content)
        dest = tmp_path / "local"
        dest.mkdir()
        out = json.loads(filehop.unpack_result(str(tar_path), str(dest)))
        assert open(out["files"][0]["path"], "rb").read() == b"result-bytes"
        assert not os.path.exists(d)

    async def test_without_header_result_stays_json(self, gateway):
        app, _seen, state, _spec = gateway
        d = tempfile.mkdtemp(prefix="awm-social-")
        f = os.path.join(d, "got.bin")
        open(f, "wb").close()
        state["result_files"] = [f]
        async with _asgi(app) as c:
            r = await c.post("/invoke", json=_domain_call(path="p"))
        assert json.loads(r.json()["result"])["files"][0]["path"] == f
        assert os.path.exists(f)
        filehop.remove_sent([f])


# -- MCP proxy, end to end into the gateway ----------------------------------

@pytest.fixture()
def proxy_to(monkeypatch, gateway):
    """Point awm-mcp's peer client at the gateway app in-process."""
    from awm import gatewayclient
    from awm.gateway import mcp_server

    app = gateway[0]
    real = httpx.AsyncClient
    sent: list = []

    def client(*a, **kw):
        kw.pop("verify", None)
        transport = httpx.ASGITransport(app=app)

        async def record(request):
            sent.append(request)

        return real(*a, transport=transport, event_hooks={"request": [record]}, **kw)

    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", client)
    monkeypatch.setattr(gatewayclient, "fetch_peer_cred", lambda alias, force=False: "tok")
    monkeypatch.setattr(gatewayclient, "_peer_ca", lambda: None)
    mcp_server._spec_cache.clear()
    return mcp_server, sent, gateway


class TestProxy:
    async def _describe_ok(self, monkeypatch, gateway, schema):
        from awm.gateway import catalog

        async def dispatch(name, args, as_=None, _orig=catalog.dispatch):
            if args.get("verb") == "describe":
                return json.dumps({"domain": name, "verbs": [
                    {"verb": args["args"]["verb"], "params": schema}]})
            return await _orig(name, args, as_)
        monkeypatch.setattr(catalog, "dispatch", dispatch)

    async def test_put_sends_local_file_in_one_call(self, proxy_to, monkeypatch, tmp_path):
        mcp_server, sent, gateway = proxy_to
        await self._describe_ok(monkeypatch, gateway, {"properties": {
            "src": {"type": "string", filehop.FILE_KEY: "in"}}, filehop.TIMEOUT_KEY: 3600})
        f = tmp_path / "local.bin"
        f.write_bytes(b"from-altair")
        out = await mcp_server._peer_invoke(
            "mira", "social", {"verb": "bucket_put", "args": {"src": str(f), "path": "p"}},
            None, entry={"edge_url": "https://mira:1", "ssh_alias": "mira"})
        assert "result" in out
        assert gateway[1][-1]["content"] == b"from-altair"
        assert sent[-1].headers["content-type"] == filehop.CONTENT_TYPE
        assert filehop.RETURN_FILES_HEADER.lower() not in sent[-1].headers

    async def test_get_brings_files_back_local(self, proxy_to, monkeypatch):
        mcp_server, _sent, gateway = proxy_to
        await self._describe_ok(monkeypatch, gateway, {"properties": {},
                                                       filehop.RETURNS_KEY: True})
        d = tempfile.mkdtemp(prefix="awm-social-")
        far = os.path.join(d, "far.bin")
        with open(far, "wb") as fh:
            fh.write(b"from-mira")
        gateway[2]["result_files"] = [far]
        out = await mcp_server._peer_invoke(
            "mira", "social", {"verb": "bucket_get", "args": {"path": "p"}},
            None, entry={"edge_url": "https://mira:1", "ssh_alias": "mira"})
        local = json.loads(out["result"])["files"][0]["path"]
        assert local != far and open(local, "rb").read() == b"from-mira"
        assert os.path.basename(local) == "far.bin"
        filehop.remove_sent([local])

    async def test_verb_without_files_stays_json(self, proxy_to, monkeypatch):
        mcp_server, sent, gateway = proxy_to
        await self._describe_ok(monkeypatch, gateway, {"properties": {
            "path": {"type": "string"}}})
        await mcp_server._peer_invoke(
            "mira", "social", {"verb": "bucket_ls", "args": {"path": "p"}},
            None, entry={"edge_url": "https://mira:1", "ssh_alias": "mira"})
        assert sent[-1].headers["content-type"] == "application/json"
        # the spec is cached: a second call does not describe again
        n = len(sent)
        await mcp_server._peer_invoke(
            "mira", "social", {"verb": "bucket_ls", "args": {"path": "p"}},
            None, entry={"edge_url": "https://mira:1", "ssh_alias": "mira"})
        assert len(sent) == n + 1

    async def test_old_peer_without_markers_gets_plain_json(self, proxy_to, monkeypatch, tmp_path):
        mcp_server, sent, gateway = proxy_to
        await self._describe_ok(monkeypatch, gateway, {"properties": {
            "src": {"type": "string"}}})
        f = tmp_path / "local.bin"
        f.write_bytes(b"x")
        await mcp_server._peer_invoke(
            "mira", "social", {"verb": "bucket_put", "args": {"src": str(f)}},
            None, entry={"edge_url": "https://mira:1", "ssh_alias": "mira"})
        assert sent[-1].headers["content-type"] == "application/json"
        assert gateway[1][-1]["args"]["args"]["src"] == str(f)
