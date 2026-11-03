"""Isolated sessions (fleet.md R109), against a fake docker and a fake host login.

No test reads the real login: CLAUDE_CONFIG_DIR points every read at a temporary one.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from awm.cch import config, hub_adapter, isolated, lifecycle

HOUR_MS = 3_600_000


def run(verb, args, as_=None):
    fn = hub_adapter.HANDLERS[verb]
    out = fn(args, as_) if len(inspect.signature(fn).parameters) == 2 else fn(args)
    return asyncio.run(out) if asyncio.iscoroutine(out) else out


def _login(expires_ms: int) -> dict:
    return {"claudeAiOauth": {"accessToken": "fake-access", "refreshToken": "fake-refresh",
                              "expiresAt": expires_ms, "scopes": ["user:inference"],
                              "subscriptionType": "max"}}


class Docker:
    """A fake docker CLI: records each call and keeps which containers run."""

    def __init__(self):
        self.calls: list[tuple[list[str], dict]] = []
        self.up: set[str] = set()
        self.fail_run = False

    def __call__(self, argv, env):
        self.calls.append((list(argv), dict(env)))
        verb, rest = argv[1], argv[2:]
        code, out = 0, ""
        if verb == "run":
            if self.fail_run:
                return subprocess.CompletedProcess(argv, 125, "", "no such image")
            self.up.add(rest[rest.index("--name") + 1])
        elif verb == "rm":
            self.up.discard(rest[-1])
        elif verb == "inspect":
            out = "true\n" if rest[-1] in self.up else "false\n"
        elif verb == "exec" and rest[0] not in self.up:
            code = 1
        return subprocess.CompletedProcess(argv, code, out, "")

    def verbs(self):
        return [argv[1] for argv, _ in self.calls]


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A fake host: its login, its Claude binary, a worktree and a fake docker."""
    claude_dir = tmp_path / "host-claude"
    claude_dir.mkdir()
    login = claude_dir / ".credentials.json"
    now = int(__import__("time").time() * 1000)
    login.write_text(json.dumps(_login(now + 8 * HOUR_MS)))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.setenv("AWM_ISOLATED_DIR", str(tmp_path / "iso"))
    monkeypatch.delenv("AWM_ISOLATED_DOCKER_HOST", raising=False)
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    binary = tmp_path / "bin" / "claude"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setattr(config, "claude_bin", lambda: str(binary))

    bare = tmp_path / "projects" / "awm" / ".bare"
    (bare / "worktrees" / "x").mkdir(parents=True)
    (bare / "worktrees" / "x" / "commondir").write_text("../..\n")
    worktree = tmp_path / "projects" / "awm" / "x"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {bare / 'worktrees' / 'x'}\n")

    async def resolve(project, scope):
        return worktree

    monkeypatch.setattr(lifecycle, "_resolve_worktree", resolve)
    monkeypatch.setattr(lifecycle, "station_refusal", lambda: None)
    fake = Docker()
    monkeypatch.setattr(isolated, "runner", fake)
    return type("Host", (), {"login": login, "binary": binary, "worktree": worktree,
                             "bare": bare, "docker": fake, "now": now})


# --- the login copy -------------------------------------------------------------


def test_the_copy_keeps_the_access_token_and_drops_the_refresh_token():
    copy = isolated.strip_login(_login(5))
    assert copy["claudeAiOauth"]["accessToken"] == "fake-access"
    assert "refreshToken" not in copy["claudeAiOauth"]
    assert isolated.strip_login({"claudeAiOauth": {"refreshToken": "r"}}) is None
    assert isolated.strip_login(None) is None


def test_sync_writes_a_private_copy_then_leaves_it_alone(host, tmp_path):
    dest = tmp_path / "c" / ".credentials.json"
    assert isolated.sync_login(dest) == "written"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert "fake-refresh" not in dest.read_text()
    assert isolated.sync_login(dest) == "unchanged"


def test_a_missing_or_expired_host_login_signs_the_copy_out(host, tmp_path):
    dest = tmp_path / "c" / ".credentials.json"
    isolated.sync_login(dest)
    assert isolated.sync_login(dest, now_ms=host.now + 9 * HOUR_MS) == "signed_out"
    assert not dest.exists()
    isolated.sync_login(dest)
    host.login.unlink()
    assert isolated.sync_login(dest) == "signed_out" and not dest.exists()
    assert not host.login.exists()


def test_a_host_refresh_reaches_every_copy(host):
    out = run("start", {"project": "awm", "scope": "x", "isolated": True})
    sid = out["claude_session_id"]
    copy = isolated.config_dir(sid) / ".credentials.json"
    fresh = _login(host.now + 16 * HOUR_MS)
    fresh["claudeAiOauth"]["accessToken"] = "fake-access-2"
    host.login.write_text(json.dumps(fresh))
    assert isolated.sync_all() == {sid: "written"}
    assert json.loads(copy.read_text())["claudeAiOauth"]["accessToken"] == "fake-access-2"


# --- caller tokens --------------------------------------------------------------


def test_a_token_is_kept_only_as_its_digest_and_dies_with_revoke(host):
    token = isolated.mint({"claude_session_id": "s1", "name": "n"})
    assert token not in isolated._tokens_path().read_text()
    assert isolated.caller_of_token(token)["name"] == "n"
    assert isolated.caller_of_token("guess") is None and isolated.caller_of_token(None) is None
    assert isolated.revoke("s1") == 1
    assert isolated.caller_of_token(token) is None


# --- the container --------------------------------------------------------------


def test_only_rootless_docker_is_accepted(monkeypatch):
    monkeypatch.setenv("AWM_ISOLATED_DOCKER_HOST", "unix:///var/run/docker.sock")
    with pytest.raises(isolated.IsolationError):
        isolated.docker_host()
    monkeypatch.delenv("AWM_ISOLATED_DOCKER_HOST")
    assert isolated.docker_host() == f"unix:///run/user/{os.getuid()}/docker.sock"


def test_the_container_sees_the_binary_read_only_and_no_host_secrets(host):
    spec = isolated.spec_from_env(claude_session_id="0" * 8 + "-0000-0000-0000-" + "0" * 12,
                                  name="n", worktree=host.worktree, claude_bin=host.binary,
                                  prompt="go")
    argv = isolated.run_argv(spec)
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert f"{host.binary}:{isolated.CLAUDE_IN}:ro" in mounts
    assert f"{host.worktree}:{host.worktree}" in mounts
    assert f"{host.bare.resolve()}:{host.bare.resolve()}" in mounts
    sources = [m.split(":")[0] for m in mounts]
    assert not any(s.endswith((".ssh", ".claude", "host-claude")) for s in sources)
    for flag in ("--cap-drop", "--security-opt", "--cpus", "--memory", "--pids-limit"):
        assert flag in argv
    assert "IS_SANDBOX=1" in argv and "--strict-mcp-config" in argv
    assert argv[-2:] == ["--", "go"]


def test_start_runs_the_container_with_a_token_the_gateway_can_check(host):
    out = run("start", {"project": "awm", "scope": "x", "name": "iso", "isolated": True,
                        "prompt": "hello"})
    assert out["ok"] and out["isolated"] and out["session_id"].startswith("cc")
    assert host.docker.verbs() == ["rm", "run"]
    argv, env = host.docker.calls[-1]
    assert "SSH_AUTH_SOCK" not in env and env["DOCKER_HOST"].startswith("unix:///run/user/")
    cfg = isolated.config_dir(out["claude_session_id"])
    mcp = json.loads((cfg / "awm-mcp.json").read_text())["mcpServers"]["awm"]
    token = mcp["headers"]["X-Awm-Caller-Token"]
    who = run("caller_of_token", {"token": token})
    assert who["known"] and who["name"] == "iso" and who["trust"] == "untrusted"
    assert who["tools"]["journal"] and "message" not in who["tools"]["reflection"]
    assert "start_other" not in who["tools"]["reflection"]
    assert "refreshToken" not in (cfg / ".credentials.json").read_text()
    state = json.loads((cfg / ".claude.json").read_text())
    assert state["projects"][str(host.worktree)]["hasTrustDialogAccepted"] is True


def test_the_contract_verbs_reach_an_isolated_session_by_its_id(host):
    out = run("start", {"project": "awm", "scope": "x", "name": "iso", "isolated": True})
    sid = out["session_id"]
    assert run("find", {"ref": "iso"})["matches"][0]["kind"] == "isolated"
    assert run("status", {"session_id": sid})["state"] == "running"
    assert run("send", {"session_id": sid, "text": "hi"})["ok"]
    assert host.docker.calls[-1][0][-2:] == ["tmux", "send-keys", "-t", "main", "Enter"][-2:]
    assert run("send", {"session_id": sid, "text": "a\nb"})["ok"] is False
    assert run("interrupt", {"session_id": sid})["ok"]
    assert run("set", {"session_id": sid, "model": "opus"})["unsupported"]
    stopped = run("stop", {"session_id": sid})
    assert stopped["stopped"] and stopped["kept"]
    assert run("status", {"session_id": sid})["state"] == "stopped"
    assert run("resume", {"session_id": sid})["resumed"]
    argv = host.docker.calls[-1][0]
    assert "--resume" in argv and "--session-id" not in argv


def test_stop_revokes_the_token_and_without_keep_removes_the_session(host):
    out = run("start", {"project": "awm", "scope": "x", "isolated": True})
    cfg = isolated.config_dir(out["claude_session_id"])
    token = json.loads((cfg / "awm-mcp.json").read_text())["mcpServers"]["awm"][
        "headers"]["X-Awm-Caller-Token"]
    run("stop", {"session_id": out["session_id"], "keep": False})
    assert run("caller_of_token", {"token": token}) == {"ok": True, "known": False}
    assert not isolated.session_dir(out["claude_session_id"]).exists()


def test_no_host_login_refuses_the_start_and_leaves_no_token(host):
    host.login.unlink()
    out = run("start", {"project": "awm", "scope": "x", "isolated": True})
    assert out["ok"] is False and "login" in out["reason"]
    assert "run" not in host.docker.verbs()
    assert isolated._read_tokens() == {}


def test_a_failed_docker_run_leaves_no_token(host):
    host.docker.fail_run = True
    out = run("start", {"project": "awm", "scope": "x", "isolated": True})
    assert out["ok"] is False and "no such image" in out["reason"]
    assert isolated._read_tokens() == {}


def test_a_peer_that_is_not_domestic_cannot_start_one(host):
    out = run("start", {"project": "awm", "scope": "x", "isolated": True}, as_="peer")
    assert out["ok"] is False and "unverified" in out["reason"]
    assert host.docker.calls == []


def test_list_shows_live_isolated_sessions_beside_the_rest(host, monkeypatch):
    async def none(args, as_=None):
        return {"ok": True, "sessions": []}

    monkeypatch.setattr(hub_adapter.cx_adapter, "_list", none)
    out = run("start", {"project": "awm", "scope": "x", "isolated": True})
    rows = run("list", {})["sessions"]
    assert [r["session_id"] for r in rows] == [out["session_id"]]
    run("stop", {"session_id": out["session_id"]})
    assert run("list", {})["sessions"] == []
    assert len(run("list", {"include_stopped": True})["sessions"]) == 1
