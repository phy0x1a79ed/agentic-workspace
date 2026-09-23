import os
import sys
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from PIL.PngImagePlugin import PngInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "view"))

import server  # noqa: E402


@pytest.fixture
def client(tmp_path):
    out = tmp_path / "outputs" / "txt2img-images" / "2024-01-01"
    out.mkdir(parents=True)
    for i in range(3):
        info = PngInfo()
        info.add_text("parameters", f"p{i} <lora:x:0.5>\nSteps: 1, Sampler: E, CFG scale: 2, Seed: {i}, Size: 64x64")
        p = out / f"0000{i}-{i}.png"
        Image.new("RGB", (64, 64)).save(p, pnginfo=info)
        os.utime(p, (time.time() - 100 + i,) * 2)
    app = server.create_app(str(tmp_path / "outputs"), str(tmp_path / "data"))
    with TestClient(app) as c:
        for _ in range(100):
            if c.get("/api/tree").json()["last_scan"]:
                break
            time.sleep(0.05)
        yield c


def test_index_injects_forwarded_prefix(client):
    body = client.get("/", headers={"X-Forwarded-Prefix": "/1111-view"}).text
    assert '<base href="/1111-view/">' in body
    assert "{{" not in body
    assert '<base href="/">' in client.get("/").text


def test_index_escapes_prefix(client):
    body = client.get("/", headers={"X-Forwarded-Prefix": '/x"><script>'}).text
    assert "<script>\"" not in body and "&quot;" in body


def test_static_assets(client):
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_tree_images_meta_thumb_file(client):
    tree = client.get("/api/tree").json()
    assert {d["path"]: d["total"] for d in tree["dirs"]}["txt2img-images/2024-01-01"] == 3
    page = client.get("/api/images", params={"source": "dir:txt2img-images", "limit": 2}).json()
    assert page["total"] == 3 and len(page["items"]) == 2
    first = page["items"][0]["id"]
    img = client.get(f"/api/image/{first}").json()
    assert img["meta"]["prompt"] == "p2 <lora:x:0.5>"
    assert img["meta"]["loras"] == [{"name": "x", "weight": 0.5}]
    t = client.get(f"/thumb/{first}")
    assert t.status_code == 200 and t.headers["content-type"] == "image/webp"
    assert client.get(f"/file/{first}").content[:4] == b"\x89PNG"
    assert client.get("/file/99999").status_code == 400


def test_folder_and_trash_flow(client):
    ids = [i["id"] for i in client.get("/api/images").json()["items"]]
    fid = client.post("/api/folders", json={"name": "A"}).json()["id"]
    gid = client.post("/api/folders", json={"name": "B"}).json()["id"]
    assert client.post("/api/folders", json={"name": "A"}).status_code == 400
    assert client.post(f"/api/folders/{fid}/add", json={"ids": ids[:2]}).json() == {"added": 2}
    client.post(f"/api/folders/{gid}/move", json={"from": fid, "ids": [ids[0]]})
    assert [i["id"] for i in client.get("/api/images", params={"source": f"folder:{gid}"}).json()["items"]] == [ids[0]]
    client.patch(f"/api/folders/{gid}", json={"name": "B2", "parent_id": fid})
    assert {f["name"]: f["parent_id"] for f in client.get("/api/tree").json()["folders"]} == {"A": None, "B2": fid}
    assert client.post("/api/trash", json={"ids": [ids[0]]}).json() == {"trashed": 1}
    assert client.get("/api/tree").json()["trash"] == 1
    assert client.get(f"/thumb/{ids[0]}").status_code == 200
    assert client.post("/api/restore", json={"ids": [ids[0]]}).json()["restored"] == 1
    client.post("/api/trash", json={"ids": [ids[1]]})
    assert client.post("/api/purge", json={"ids": None}).json() == {"purged": 1}
    assert client.get("/api/images").json()["total"] == 2
    assert client.delete(f"/api/folders/{fid}").json() == {"ok": True}
    assert client.get("/api/tree").json()["folders"] == []
