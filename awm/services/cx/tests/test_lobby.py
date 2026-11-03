"""Starts in the lobby, suggested routes and task ids (scopes.md R1, R40; sessions.md R18, R56, R57)."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess

import pytest

from test_lifecycle import (BASE, CHILD_JOB, PARENT_JOB, home, launched, no_link_wait,  # noqa: F401
                            parent_session, start, system_prompt)

UUID = "0123abcd-4567-89ef-0123-456789abcdef"
R9 = "cc0123abcd456789ef0123456789abcdef"


@pytest.fixture
def lobby(tmp_path, monkeypatch, home):
    path = tmp_path / "lobby"
    monkeypatch.setenv("AWM_CX_LOBBY", str(path))
    return path


@pytest.fixture
def with_session_id(launched, monkeypatch):
    """The launched session reports a Claude session id, as the daemon's does."""
    from awm.cx import lifecycle

    inner = lifecycle.launch.launch

    async def launch(**kw):
        return dataclasses.replace(await inner(**kw), session_id=UUID)

    monkeypatch.setattr(lifecycle.launch, "launch", launch)
    return launched


@pytest.fixture
def attached(monkeypatch):
    from awm.cx import lifecycle

    calls: list[tuple] = []

    async def call(service, fn, args, timeout=None):
        calls.append((service, fn, args))
        return {"ok": True, "task_id": args.get("task_id")}

    monkeypatch.setattr(lifecycle.gatewayclient, "call", call)
    return calls


def test_no_scope_starts_in_the_lobby(lobby, launched, home):
    out = start({"prompt": "route yourself"})
    assert out["ok"] and out["lobby"] is True and out["cwd"] == str(lobby)
    assert (out["project"], out["scope"]) == (None, None)
    assert out["name"].startswith("lobby-")
    text = system_prompt(launched[0])
    assert "You started in the lobby" in text and "Find your home first" in text
    assert "new_project:true" in text


def test_the_lobby_loads_the_awm_tools_and_the_workspace_orientation(lobby, launched, home):
    start({})
    flags = launched[0]["flags"]
    mcp = [f for f in flags if f.startswith("--mcp-config=")]
    assert mcp and "awm" in json.loads(open(mcp[0].partition("=")[2]).read())["mcpServers"]
    assert "--strict-mcp-config" not in flags
    assert "AGENTS.md" in (lobby / "CLAUDE.md").read_text()


def test_the_lobby_is_trusted_by_its_own_entry(lobby, launched, home):
    start({})
    trust = json.loads(open(os.environ["AWM_CX_TRUST_FILE"]).read())
    assert trust["projects"][str(lobby.resolve())]["hasTrustDialogAccepted"] is True


def test_a_lobby_inside_a_repository_is_refused(tmp_path, monkeypatch, launched, home):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setenv("AWM_CX_LOBBY", str(repo / "lobby"))
    out = start({})
    assert out["ok"] is False and "inside a git repository" in out["reason"]
    assert launched == []


def test_a_suggested_scope_starts_in_the_lobby_and_names_the_route(lobby, launched, home):
    out = start({**BASE, "suggest": True, "name": "fix/follow-up"})
    assert out["lobby"] is True and out["cwd"] == str(lobby)
    assert out["suggested"] == {"project": "awm", "scope": "demo"}
    text = system_prompt(launched[0])
    assert "suggests awm/demo" in text and "Choose another scope" in text
    from awm.cx import lifecycle

    assert lifecycle.read_lineage(CHILD_JOB)["suggested"] == {"project": "awm", "scope": "demo"}


def test_a_project_alone_is_a_hint(lobby, launched, home):
    start({"project": "awm"})
    assert "suggests the project awm" in system_prompt(launched[0])


def test_a_scope_without_its_project_is_refused(lobby, launched, home):
    out = start({"scope": "demo"})
    assert out["ok"] is False and "project" in out["reason"]


def test_a_named_scope_still_starts_there(lobby, launched, home):
    out = start(BASE)
    assert out["lobby"] is False and out["cwd"] == str(home.projects / "awm" / "demo")
    assert "the scope your starter chose" in system_prompt(launched[0])


def test_the_brief_covers_naming_top_level_starts_and_subagents(lobby, launched, home):
    start({})
    text = system_prompt(launched[0])
    assert "`<task>/<descriptor>`" in text and "parallel-work" in text
    assert "do not call `EnterWorktree`" in text and "subagent" in text
    assert "Tony does not accept the work first" in text


def test_a_task_id_from_a_service_attaches_the_session_as_main(lobby, with_session_id, home, attached):
    out = start({**BASE, "task_id": "ABEiM0RV"})
    assert out["session_id"] == R9 and (out["task_id"], out["role"]) == ("ABEiM0RV", "main")
    assert ("reporting", "task_attach",
            {"task_id": "ABEiM0RV", "session_id": R9, "role": "main", "name": "demo"}) in attached
    assert "You joined task `ABEiM0RV` as its main" in system_prompt(with_session_id[0])
    from awm.cx import lifecycle

    rec = lifecycle.read_lineage(CHILD_JOB)
    assert (rec["task_id"], rec["task_role"]) == ("ABEiM0RV", "main")


def test_a_task_id_from_a_session_attaches_a_worker(lobby, with_session_id, home, attached):
    parent_session(home)
    out = start({**BASE, "task_id": "ABEiM0RV", "_caller_pid": os.getpid()})
    assert out["role"] == "worker" and out["parent"] == PARENT_JOB
    assert attached[-1][2]["role"] == "worker"


def test_an_attach_failure_is_a_warning(lobby, with_session_id, home, monkeypatch):
    from awm.cx import lifecycle

    async def down(*a, **k):
        raise ConnectionError("no gateway")

    monkeypatch.setattr(lifecycle.gatewayclient, "call", down)
    out = start({**BASE, "task_id": "ABEiM0RV"})
    assert out["ok"] is True and "not put on task ABEiM0RV" in out["warning"]


@pytest.mark.parametrize("args,why", [
    ({"task_id": "short"}, "task id"),
    ({"task_id": "ABEiM0RV", "role": "boss"}, "role must be"),
    ({"role": "main"}, "role needs a task_id"),
])
def test_bad_task_arguments_are_refused(lobby, launched, home, args, why):
    out = start({**BASE, **args})
    assert out["ok"] is False and why in out["reason"]


def test_a_start_from_a_session_records_its_parent_link(lobby, with_session_id, home, attached):
    parent_session(home)
    for pid_file in home.sessions.glob("*.json"):
        rec = json.loads(pid_file.read_text())
        rec["sessionId"] = "ffffffff-0000-1111-2222-333333333333"
        pid_file.write_text(json.dumps(rec))
    start({**BASE, "_caller_pid": os.getpid()})
    parent = "ccffffffff000011112222333333333333"
    assert ("reporting", "session_parent", {"session_id": R9, "parent": parent, "how": "start"}) in attached
    from awm.cx import lifecycle

    assert lifecycle.read_lineage(CHILD_JOB)["parent_session"] == parent
