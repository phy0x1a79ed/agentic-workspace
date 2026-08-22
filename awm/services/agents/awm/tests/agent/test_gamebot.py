"""Game-bot runtime — registry loading, spawn/dedupe/park lifecycle, and the
turn driver (budget countdown → save-and-park escalation → force-park)."""

from __future__ import annotations

import asyncio

import pytest

pytestmark = [pytest.mark.agent, pytest.mark.smoke]

from awm.agents import gamebot
from awm.agents import agent_instances as ai


FACTORIO_TOML = """
realm = "rlm-factorio"
harness = "opencode"
turn_budget = 7
goal = "Smelt iron plates."
"""


@pytest.fixture
def games_dir(tmp_path, monkeypatch):
    root = tmp_path / "games"
    root.mkdir()
    (root / "factorio.toml").write_text(FACTORIO_TOML)
    monkeypatch.setenv("AWM_GAMES_DIR", str(root))
    return root


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_load_game(self, games_dir):
        spec = gamebot.load_game("factorio")
        assert spec.realm == "rlm-factorio"
        assert spec.harness == "opencode"
        assert spec.model is None  # → the opencode backend's own default
        assert spec.turn_budget == 7
        assert "iron plates" in spec.goal

    def test_unknown_game(self, games_dir):
        with pytest.raises(gamebot.UnknownGameError):
            gamebot.load_game("nope")

    def test_goal_required(self, games_dir):
        (games_dir / "bad.toml").write_text('realm = "rlm-x"\n')
        with pytest.raises(gamebot.GamebotError):
            gamebot.load_game("bad")

    def test_list_games(self, games_dir):
        assert gamebot.list_games() == ["factorio"]

    def test_brief_mentions_journal_save_and_park(self, games_dir):
        brief = gamebot.render_game_brief(gamebot.load_game("factorio"))
        assert "journal.md" in brief
        assert "park" in brief
        assert "Smelt iron plates." in brief
        assert "rlm" in brief


# ---------------------------------------------------------------------------
# Spawn / dedupe / park
# ---------------------------------------------------------------------------

async def _drain(seconds: float = 0.1):
    await asyncio.sleep(seconds)


class TestSpawnLifecycle:
    @pytest.mark.asyncio
    async def test_spawn_provisions_unit_and_session(
            self, agents_env, stub_core, games_dir):
        res = await gamebot.spawn_for_game("factorio", source="test")
        assert res["spawned"] is True
        assert res["scope"] == "game-factorio"
        # The unit was provisioned before the spawn, keyed on the slug.
        ws_calls = [c for c in agents_env["calls"]
                    if c[1] == "workspace_create"]
        assert ws_calls and ws_calls[0][2]["unit_slug"] == "game-factorio"
        # The session runs the registry's driver + budget, in gamebot mode.
        session = ai.get_session_by_scope("game-factorio")
        assert session is not None
        assert session.agent_cli == "opencode"
        assert session.mode == "gamebot"
        assert session.turn_budget == 7
        assert session.placement_token is None
        # Kickoff reached the harness.
        await _drain()
        sent = stub_core["session"].sent
        assert sent and "journal.md" in sent[0]

    @pytest.mark.asyncio
    async def test_second_spawn_dedupes(self, agents_env, stub_core, games_dir):
        first = await gamebot.spawn_for_game("factorio")
        assert first["spawned"] is True
        second = await gamebot.spawn_for_game("factorio", source="schedule.tick")
        assert second["spawned"] is False
        assert second["reason"] == "already playing"
        # Only one workspace_create.
        assert len([c for c in agents_env["calls"]
                    if c[1] == "workspace_create"]) == 1

    @pytest.mark.asyncio
    async def test_park_stops_the_session(self, agents_env, stub_core, games_dir):
        await gamebot.spawn_for_game("factorio")
        res = await gamebot.park({}, as_="game-factorio")
        assert res["parked"] is True
        for _ in range(50):
            if ai.get_session_by_scope("game-factorio") is None:
                break
            await asyncio.sleep(0.02)
        assert ai.get_session_by_scope("game-factorio") is None
        # Parked = intent "stopped", so nothing reads as a crash.
        row = ai._get_dao().get_instance(res["session_id"])
        assert row is not None and row.get("ended_at") is not None

    @pytest.mark.asyncio
    async def test_park_requires_identity(self, agents_env, stub_core, games_dir):
        with pytest.raises(gamebot.GamebotError):
            await gamebot.park({}, as_=None)

    @pytest.mark.asyncio
    async def test_park_without_session_is_soft(self, agents_env, games_dir):
        res = await gamebot.park({}, as_="game-factorio")
        assert res["parked"] is False


# ---------------------------------------------------------------------------
# Turn driver
# ---------------------------------------------------------------------------

class TestTurnDriver:
    @pytest.mark.asyncio
    async def test_nudge_and_escalation(self, agents_env, stub_core, games_dir):
        await gamebot.spawn_for_game("factorio")
        session = ai.get_session_by_scope("game-factorio")
        await _drain()
        n_sent = len(stub_core["session"].sent)

        # Plenty of budget → a plain continuation nudge.
        session.turn_budget = gamebot.WARN_REMAINING + 2
        await gamebot.on_turn_boundary(session)
        await _drain()
        sent = stub_core["session"].sent
        assert len(sent) == n_sent + 1
        assert "[supervisor]" in sent[-1] and "park" in sent[-1]
        assert "Do not start new work" not in sent[-1]

        # Near zero → the save-and-park escalation.
        session.turn_budget = 2
        await gamebot.on_turn_boundary(session)
        await _drain()
        assert "Do not start new work" in stub_core["session"].sent[-1]

    @pytest.mark.asyncio
    async def test_force_park_at_zero(self, agents_env, stub_core, games_dir):
        await gamebot.spawn_for_game("factorio")
        session = ai.get_session_by_scope("game-factorio")
        session.turn_budget = 1
        await gamebot.on_turn_boundary(session)
        for _ in range(50):
            if ai.get_session_by_scope("game-factorio") is None:
                break
            await asyncio.sleep(0.02)
        assert ai.get_session_by_scope("game-factorio") is None

    @pytest.mark.asyncio
    async def test_parked_session_noops(self, agents_env, stub_core, games_dir):
        await gamebot.spawn_for_game("factorio")
        session = ai.get_session_by_scope("game-factorio")
        await gamebot.park({}, as_="game-factorio")
        for _ in range(50):
            if ai.get_session_by_scope("game-factorio") is None:
                break
            await asyncio.sleep(0.02)
        budget = session.turn_budget
        await gamebot.on_turn_boundary(session)
        assert session.turn_budget == budget  # no decrement after park
