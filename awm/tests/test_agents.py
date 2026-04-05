"""Tests for the agent spawning service."""

from __future__ import annotations

from unittest.mock import MagicMock, mock_open, patch

import pytest

from awm.models import AgentSpawnRequest
from awm.services import agents


@pytest.fixture()
def scope_workspace(awm_workspace, seeded_scopes):
    """Ensure scope workspaces exist."""
    return awm_workspace


class TestSpawnAgent:
    def test_spawn_opencode(self, scope_workspace):
        req = AgentSpawnRequest(project="proj-a", scope="scope-1", agent_cli="opencode")
        workspace_dir = scope_workspace["projects_dir"] / "proj-a" / "scope-1"

        with patch("awm.services.agents.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=12345)
            resp = agents.spawn_agent(req)

        assert resp.pid == 12345
        assert resp.agent_cli == "opencode"
        call_args = mock_popen.call_args
        assert call_args[0][0] == ["opencode"]
        assert call_args[1]["cwd"] == str(workspace_dir)
        assert call_args[1]["start_new_session"] is True

    def test_spawn_claude_with_prompt(self, scope_workspace):
        req = AgentSpawnRequest(
            project="proj-a", scope="scope-1",
            prompt="Implement feature X",
            agent_cli="claude",
        )

        with patch("awm.services.agents.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=54321)
            resp = agents.spawn_agent(req)

        assert resp.agent_cli == "claude"
        call_args = mock_popen.call_args
        assert call_args[0][0] == ["claude", "--print", "-p", "Implement feature X"]

    def test_spawn_creates_inbox_message(self, scope_workspace):
        req = AgentSpawnRequest(
            project="proj-a", scope="scope-1",
            prompt="Do the work",
            agent_cli="opencode",
        )

        with patch("awm.services.agents.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=100)
            agents.spawn_agent(req)

        # Verify inbox message was created
        from awm.services import messaging

        result = messaging.search_messages(scope="scope:proj-a/scope-1", msg_type="plan")
        assert result.total >= 1
        assert "Do the work" in result.messages[0].body

    def test_spawn_missing_scope_raises(self, scope_workspace):
        req = AgentSpawnRequest(project="proj-a", scope="nonexistent", agent_cli="opencode")
        with pytest.raises(FileNotFoundError):
            agents.spawn_agent(req)

    def test_spawn_unknown_cli_raises(self, scope_workspace):
        req = AgentSpawnRequest(project="proj-a", scope="scope-1", agent_cli="unknown")
        with pytest.raises(ValueError, match="Unknown agent CLI"):
            agents.spawn_agent(req)

    def test_spawn_config_fallback(self, scope_workspace):
        """When no agent_cli is specified, falls back to config table default."""
        from awm.services import config_service
        config_service.set_config("agent_cli", "opencode")

        req = AgentSpawnRequest(project="proj-a", scope="scope-1")

        with patch("awm.services.agents.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=200)
            resp = agents.spawn_agent(req)

        assert resp.agent_cli == "opencode"

    def test_spawn_no_prompt_no_inbox_message(self, scope_workspace):
        req = AgentSpawnRequest(project="proj-a", scope="scope-1", agent_cli="opencode")

        with patch("awm.services.agents.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=300)
            agents.spawn_agent(req)

        from awm.services import messaging
        result = messaging.search_messages(scope="scope:proj-a/scope-1", msg_type="plan")
        assert result.total == 0
