"""A service's call names its plugin by the token its spawn got, and its needs bound it (R116 item 3)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from awm import config
from awm.gateway import server
from awm.gateway.hub import discovery, supervisor


def test_a_call_names_its_tool_and_verb():
    assert server._call_target("journal", {"verb": "post", "args": {}}) == ("journal", "post")
    assert server._call_target("journal_post", {"body": "x"}) == ("journal", "post")
    assert server._call_target("harness-cc_daemon_ensure", {}) == ("harness-cc", "daemon_ensure")


def test_a_token_names_its_service_and_a_new_spawn_replaces_it(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AWM_DIR", tmp_path)
    first = supervisor.mint_token("journal").read_text()
    assert supervisor.service_of_token(first) == "journal"
    assert (tmp_path / "tokens" / "journal.token").stat().st_mode & 0o077 == 0
    second = supervisor.mint_token("journal").read_text()
    assert first != second
    assert supervisor.service_of_token(first) is None
    assert supervisor.service_of_token(second) == "journal"
    assert supervisor.service_of_token("") is None


def test_the_guard_refuses_an_undeclared_verb_and_a_forged_token(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AWM_DIR", tmp_path)
    token = supervisor.mint_token("caller").read_text()
    seen = []
    monkeypatch.setattr(discovery, "needs_refusal", lambda svc, tool, verb: seen.append((svc, tool, verb)) or "no")
    assert server._needs_refusal(token, "journal", {"verb": "fetch"}) == "no"
    assert seen == [("caller", "journal", "fetch")]
    with pytest.raises(HTTPException) as exc:
        server._needs_refusal("forged", "journal", {"verb": "fetch"})
    assert exc.value.status_code == 401
