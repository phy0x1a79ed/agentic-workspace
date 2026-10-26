"""board_watch and board_unwatch: who follows a card, recorded against the caller's job."""

from __future__ import annotations

import pytest

from test_tasks import Env

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_CX_PROJECTS", str(tmp_path / "projects"))
    return Env(tmp_path)


async def test_a_background_session_watches_a_card_by_id_or_prefix(env):
    a, b = env.federation_card(), env.federation_card()
    out = await env.call("board_watch", card_id=a["id"])
    assert out["ok"] and out["watching"] == [a["id"]]
    out = await env.call("board_watch", card_id=b["id"][:8])
    assert out["watching"] == [a["id"], b["id"]]
    assert env.queue.watchers_of(a["id"]) == ["job00001"]


async def test_watching_twice_records_one_watcher(env):
    card = env.federation_card()
    await env.call("board_watch", card_id=card["id"])
    out = await env.call("board_watch", card_id=card["id"])
    assert out["watching"] == [card["id"]]


async def test_posted_by_me_follows_the_sessions_name(env):
    out = await env.call("board_watch", posted_by_me=True)
    assert out["ok"] and out["watching"] == ["posted:opal"]
    assert env.queue.watchers_of("anything", posted_by="opal") == ["job00001"]


async def test_a_session_with_no_name_cannot_follow_its_posts(env):
    env.names.clear()
    out = await env.call("board_watch", posted_by_me=True)
    assert out["ok"] is False and "no name" in out["error"]


async def test_an_interactive_session_cannot_watch(env):
    card = env.federation_card()
    out = await env.call("board_watch", pid=200, card_id=card["id"])
    assert out["ok"] is False and "background session" in out["error"]
    assert env.queue.watched_by("200:77") == []


async def test_watching_needs_a_card_or_the_flag_and_a_card_the_swarm_can_see(env):
    assert (await env.call("board_watch"))["ok"] is False
    out = await env.call("board_watch", card_id="deadbeef")
    assert out["ok"] is False and "no card matches" in out["error"]
    assert env.queue.watched_by("job00001") == []


async def test_a_node_with_no_board_token_says_events_may_not_arrive(env, monkeypatch):
    card = env.federation_card()
    monkeypatch.setenv("AWM_BOARD_URL", "https://board.example")
    monkeypatch.setenv("AWM_BOARD_TOKEN", "t")
    monkeypatch.setenv("AWM_FRONT_DOOR", "0")
    assert "warning" not in await env.call("board_watch", card_id=card["id"])
    monkeypatch.delenv("AWM_BOARD_TOKEN")
    out = await env.call("board_watch", card_id=card["id"])
    assert out["ok"] and "no board token" in out["warning"]


async def test_a_session_watches_a_project_or_another_swarm(env):
    out = await env.call("board_watch", project="avatar", swarm="caviar")
    assert out["watching"] == ["project:avatar", "swarm:caviar"]
    assert env.queue.watchers_of("x", project="avatar") == ["job00001"]
    assert env.queue.watchers_of("x", swarm="caviar") == ["job00001"]
    assert env.queue.watchers_of("x", project="other", swarm="beta") == []
    out = await env.call("board_unwatch", swarm="caviar")
    assert out["watching"] == ["project:avatar"]
    assert (await env.call("board_watch", project="a:b"))["ok"] is False


async def test_unwatch_removes_one_target_or_all_of_them(env):
    a, b = env.federation_card(), env.federation_card()
    await env.call("board_watch", card_id=a["id"])
    await env.call("board_watch", card_id=b["id"])
    await env.call("board_watch", posted_by_me=True)
    out = await env.call("board_unwatch", card_id=a["id"][:8])
    assert out["watching"] == [b["id"], "posted:opal"]
    out = await env.call("board_unwatch", posted_by_me=True)
    assert out["watching"] == [b["id"]]
    assert (await env.call("board_unwatch"))["watching"] == []


async def test_watchers_of_an_ended_session_are_pruned_by_the_runtime_tick(env):
    from types import SimpleNamespace

    from awm.reporting import hub_adapter

    card = env.federation_card()
    await env.call("board_watch", card_id=card["id"])
    runtime = SimpleNamespace(queue=env.queue, tasks=env.tasks)
    await hub_adapter.Runtime._on_rows(runtime, [{"job": "other", "state": "idle"}])
    assert env.queue.watched_by("job00001") == []
    await env.call("board_watch", card_id=card["id"])
    await hub_adapter.Runtime._on_rows(runtime, [{"job": "job00001", "state": "idle"}])
    assert env.queue.watched_by("job00001") == [card["id"]]
