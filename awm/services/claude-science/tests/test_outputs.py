"""Output folders, against a stub of the workbench's project and frame routes.

What matters most here is the delete: a folder holds an agent's outputs, so it
goes only when the daemon positively answers 404 for its conversation. A failed
lookup, or a conversation missing from a listing, keeps it.
"""

from __future__ import annotations

import json

import httpx
import pytest

from awm.claude_science import api, outputs

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def _project(pid, name, created, context=None):
    return {"project_id": pid, "name": name, "created_at": created,
            "context": context}


class Stub:
    def __init__(self, projects, convs, frame_status=None):
        self.projects = projects
        self.convs = convs                      # pid -> [conversation id]
        self.frame_status = frame_status or {}  # id8 -> HTTP status
        self.patches: dict[str, str] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")
        if request.method == "GET" and request.url.path == "/api/projects":
            return httpx.Response(200, json={"projects": self.projects})
        if request.method == "GET" and parts[-1] == "benches":
            ids = self.convs.get(parts[2], [])
            return httpx.Response(200, json=[
                {"id": i, "parent_frame_id": None} for i in ids])
        if request.method == "PATCH" and parts[:2] == ["api", "projects"]:
            ctx = json.loads(request.content)["context"]
            self.patches[parts[2]] = ctx
            for p in self.projects:
                if p["project_id"] == parts[2]:
                    p["context"] = ctx
            return httpx.Response(200, json={})
        if request.method == "GET" and parts[:2] == ["api", "frames"]:
            status = self.frame_status.get(parts[2], 200)
            return httpx.Response(status, json={"id": parts[2]})
        return httpx.Response(405)

    def owner(self) -> api.Owner:
        return api.Owner("tok", transport=httpx.MockTransport(self))


PROJECTS = [
    _project("proj_aaaaaa111111", "Metals Fleet", "2026-01-01"),
    _project("proj_bbbbbb222222", "Agents", "2026-02-01",
             context="Source code: /x"),
]
CONVS = {"proj_aaaaaa111111": ["1234abcd-0000"],
         "proj_bbbbbb222222": ["5678ef01-0000", "9999aaaa-0000"]}


def test_sync_creates_one_empty_folder_per_conversation(tmp_path):
    stub = Stub(PROJECTS, CONVS)
    with stub.owner() as owner:
        report = outputs.sync(owner, tmp_path)
    assert sorted(report["created"]) == [
        "agents/5678ef01", "agents/9999aaaa", "metals-fleet/1234abcd"]
    assert list((tmp_path / "agents" / "5678ef01").iterdir()) == []


def test_sync_puts_the_folder_line_in_each_project_context_once(tmp_path):
    stub = Stub(PROJECTS, CONVS)
    with stub.owner() as owner:
        outputs.sync(owner, tmp_path)
        assert stub.patches["proj_bbbbbb222222"].startswith("Source code: /x\n")
        assert f"{tmp_path}/agents/<first 8" in stub.patches["proj_bbbbbb222222"]
        stub.patches.clear()
        outputs.sync(owner, tmp_path)
    assert stub.patches == {}


def test_only_a_404_deletes_a_folder_and_its_contents(tmp_path):
    for id8 in ("deadbeef", "cafef00d"):
        folder = tmp_path / "agents" / id8
        folder.mkdir(parents=True)
        (folder / "result.csv").write_text("x")
    stub = Stub(PROJECTS, CONVS, frame_status={"deadbeef": 404,
                                               "cafef00d": 500})
    with stub.owner() as owner:
        report = outputs.sync(owner, tmp_path)
    assert report["deleted"] == ["agents/deadbeef"]
    assert not (tmp_path / "agents" / "deadbeef").exists()
    assert (tmp_path / "agents" / "cafef00d" / "result.csv").exists()


def test_a_conversation_missing_from_the_listing_but_alive_is_kept(tmp_path):
    (tmp_path / "agents" / "77777777").mkdir(parents=True)
    with Stub(PROJECTS, CONVS).owner() as owner:
        report = outputs.sync(owner, tmp_path)
    assert report["deleted"] == []
    assert (tmp_path / "agents" / "77777777").is_dir()


# ---------------------------------------------------------------------------
# The grant guard
# ---------------------------------------------------------------------------

def _grant(path, mode):
    return {"id": str(path), "hostPath": str(path), "mode": mode}


def test_a_rw_grant_inside_a_ro_grant_is_refused(tmp_path):
    inner = tmp_path / "projects" / "metasmith" / "data"
    inner.mkdir(parents=True)
    with pytest.raises(ValueError, match="overlaps"):
        outputs.check_writable_grant(
            str(inner), [_grant(tmp_path / "projects", "ro")])


def test_a_rw_grant_around_a_ro_grant_is_refused(tmp_path):
    (tmp_path / "projects").mkdir()
    with pytest.raises(ValueError, match="overlaps"):
        outputs.check_writable_grant(
            str(tmp_path), [_grant(tmp_path / "projects", "ro")])


def test_a_rw_grant_past_the_scan_cap_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(outputs, "SCAN_CAP", 5)
    for i in range(6):
        (tmp_path / f"d{i}").mkdir()
    with pytest.raises(ValueError, match="more than 5 folders"):
        outputs.check_writable_grant(str(tmp_path), [])
