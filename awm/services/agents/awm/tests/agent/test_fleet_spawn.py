"""Unit tests for fleet_spawn — command building, validation, name safety.

The real tmux launch (`spawn_terminal`'s new-session/send-keys) is exercised by
the live-hub harness; here we cover the pure logic and the validation guards
that must reject bad input before ever touching tmux."""
from __future__ import annotations

import pytest

from awm.agents import fleet_spawn as fs


class TestBuildCommand:
    def test_claude_full(self):
        assert fs.build_command("claude", "claude-sonnet-5", "high") == (
            "claude --permission-mode default --model claude-sonnet-5 --effort high")

    def test_claude_no_effort(self):
        assert fs.build_command("claude", "haiku", None) == (
            "claude --permission-mode default --model haiku")

    def test_claude_full_permissions(self):
        assert fs.build_command("claude", None, None, "full") == (
            "claude --dangerously-skip-permissions")

    def test_opencode_is_bare(self):
        assert fs.build_command("opencode", "ignored", "ignored") == "opencode"


class TestSafeName:
    def test_strips_tmux_specials(self):
        assert fs._safe_name("feat/svc.notes:x y") == "feat-svc-notes-x-y"

    def test_empty_falls_back(self):
        assert fs._safe_name("///") == "agent"


class TestValidation:
    def test_bad_cwd(self):
        with pytest.raises(FileNotFoundError):
            fs.spawn_terminal(cwd="/nope/does/not/exist", model="haiku")

    def test_bad_harness(self, tmp_path):
        with pytest.raises(ValueError, match="Unknown harness"):
            fs.spawn_terminal(cwd=str(tmp_path), harness="bogus")

    def test_claude_requires_model(self, tmp_path):
        with pytest.raises(ValueError, match="requires an explicit model"):
            fs.spawn_terminal(cwd=str(tmp_path), harness="claude")

    def test_bad_effort(self, tmp_path):
        with pytest.raises(ValueError, match="Invalid effort"):
            fs.spawn_terminal(cwd=str(tmp_path), harness="claude",
                              model="haiku", effort="ultra")


class TestKillTmux:
    def test_kill_missing_session_is_false(self):
        # A name that certainly doesn't exist → kill returns False, never raises.
        assert fs.kill_tmux_session("definitely-not-a-real-session-zzz9") is False

    def test_kill_blank_is_false(self):
        assert fs.kill_tmux_session("") is False
