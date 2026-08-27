"""Per-user notes: a bound user reads and writes their own worktree and DB,
an unbound caller gets the legacy store, strict mode refuses, and a flush
commits only ``notes/`` in the user's worktree."""

from __future__ import annotations

import subprocess

import pytest

from awm.config import autocommit, userroot
from awm.notes import config, hub_adapter, index, rooms

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def _repo(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / "README").write_text("x")
    subprocess.run(["git", "-C", str(path), *autocommit.GIT_IDENTITY, "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), *autocommit.GIT_IDENTITY, "commit", "-qm", "init"], check=True)
    return path


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    services = tmp_path / ".awm" / "services"
    services.mkdir(parents=True)
    monkeypatch.setattr("awm.config.WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr("awm.config.SERVICES_DIR", services)
    monkeypatch.setattr("awm.persistence.databases.SERVICES_DIR", services)
    monkeypatch.delenv(userroot.TEMPLATE_ENV, raising=False)
    monkeypatch.delenv(userroot.STRICT_ENV, raising=False)
    for name in ("tony", "steven"):
        _repo(tmp_path / "projects" / "userdata" / name)
    monkeypatch.setattr(index, "embed_note", lambda *a, **k: None)
    monkeypatch.setattr(index, "drop_embedding", lambda *a, **k: None)
    monkeypatch.setattr(index, "search_semantic", lambda *a, **k: [])
    monkeypatch.setattr(index, "probe", lambda: {"available": True, "missing": []})
    rooms._ROOMS.clear()
    yield tmp_path
    rooms._ROOMS.clear()


def _create(as_, path, content):
    return hub_adapter.HANDLERS["create"]({"path": path, "content": content}, as_)


def _get(as_, note_id):
    return hub_adapter.HANDLERS["get"]({"id": note_id}, as_)


def test_users_are_isolated_and_files_land_in_the_worktree(ws):
    t = _create("user:tony", "a", "tony's")
    s = _create("user:steven", "a", "steven's")
    assert t["file_path"].startswith(str(ws / "projects/userdata/tony/notes/"))
    assert s["file_path"].startswith(str(ws / "projects/userdata/steven/notes/"))
    assert _get("user:tony", t["id"])["content"] == "tony's"
    with pytest.raises(Exception):
        _get("user:steven", t["id"])
    assert (ws / ".awm/services/notes/users/tony/notes.db").is_file()
    assert (ws / ".awm/services/notes/users/steven/notes.db").is_file()


def test_unknown_identity_uses_the_legacy_store(ws):
    n = _create("placement-slug", "legacy", "x")
    assert n["file_path"].startswith(str(ws / ".awm/services/notes/files/"))
    assert _get(None, n["id"])["content"] == "x"
    assert _get("user:operator", n["id"])["content"] == "x"
    with pytest.raises(Exception):
        _get("user:tony", n["id"])


def test_strict_mode_refuses_non_users(ws, monkeypatch):
    monkeypatch.setenv(userroot.STRICT_ENV, "1")
    with pytest.raises(PermissionError):
        _create("placement-slug", "legacy", "x")
    assert _create("user:tony", "ok", "x")["id"]


def test_collab_topics_carry_the_user(ws):
    t = _create("user:tony", "a", "hi")
    res = hub_adapter.HANDLERS["collab_open"]({"id": t["id"]}, "user:tony")
    assert res["topic"] == f"note:tony:{t['id']}"
    legacy = _create(None, "b", "hi")
    res = hub_adapter.HANDLERS["collab_open"]({"id": legacy["id"]}, None)
    assert res["topic"] == f"note:{legacy['id']}"


def test_flush_commits_only_notes_in_the_users_worktree(ws):
    t = _create("user:tony", "a", "v1")
    root = ws / "projects/userdata/tony"
    (root / "drawio").mkdir()
    (root / "drawio" / "stray.drawio").write_text("<>")
    hub_adapter.HANDLERS["collab_open"]({"id": t["id"]}, "user:tony")
    import asyncio
    asyncio.run(hub_adapter.HANDLERS["collab_edit"](
        {"id": t["id"], "base_version": 0, "content": "v2"}, "user:tony"))
    flushed = hub_adapter._flush_once()
    assert flushed == [t["id"]]
    log = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%B", "--name-only"],
                         capture_output=True, text=True, check=True).stdout
    assert "Author-Handle: user:tony" in log
    assert f"notes/{t['id']}.md" in log and "drawio" not in log
    assert (root / "notes" / f"{t['id']}.md").read_text() == "v2"
    assert hub_adapter._flush_once() == []
