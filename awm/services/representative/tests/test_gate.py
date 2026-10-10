"""The enable gate: the door runs only on a fleet node with AWM_FRONT_DOOR=1."""

from __future__ import annotations

import logging

import pytest

from awm.representative import config, hub_adapter, reconcile

from stubs import FakeCx, personas

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def test_a_fleet_node_with_the_flag_may_run():
    assert config.refusal() is None


def test_the_flag_off_refuses(monkeypatch):
    monkeypatch.delenv("AWM_FRONT_DOOR")
    assert "switched off" in config.refusal()
    monkeypatch.setenv("AWM_FRONT_DOOR", "0")
    assert config.refusal() is not None
    monkeypatch.setenv("AWM_FRONT_DOOR", "true")  # only the literal 1 enables it
    assert config.refusal() is not None


def test_a_station_refuses_even_with_the_flag(monkeypatch):
    monkeypatch.setenv("AWM_NODE_ROLE", "station")
    assert "station" in config.refusal()


def test_a_bad_role_refuses_instead_of_running(monkeypatch):
    monkeypatch.setenv("AWM_NODE_ROLE", "bunker")
    assert "invalid" in config.refusal()


@pytest.mark.parametrize("env", [{"AWM_NODE_ROLE": "station"}, {"AWM_FRONT_DOOR": "0"}])
def test_start_logs_a_refusal_and_builds_nothing(monkeypatch, caplog, tmp_path, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(hub_adapter, "RUNTIME", None)
    spawned = []
    monkeypatch.setattr(hub_adapter, "spawn_supervised", lambda *a, **k: spawned.append(a))
    with caplog.at_level(logging.WARNING):
        hub_adapter._on_start()
    assert hub_adapter.RUNTIME is None
    assert spawned == []
    assert not (tmp_path / "door.db").exists()
    assert "not running" in caplog.text


async def test_the_reconcile_loop_starts_nothing_on_a_station(monkeypatch, queue):
    monkeypatch.setenv("AWM_NODE_ROLE", "station")
    cx = FakeCx()
    await reconcile.Loop(queue, cx=cx, personas=personas()).tick()
    assert cx.started == []


async def test_the_reconcile_loop_starts_nothing_with_the_flag_off(monkeypatch, queue):
    monkeypatch.setenv("AWM_FRONT_DOOR", "0")
    cx = FakeCx()
    await reconcile.Loop(queue, cx=cx, personas=personas()).tick()
    assert cx.started == []


def test_the_swarm_comes_from_the_node_and_not_a_constant(monkeypatch):
    assert config.swarm() == "tony"
    monkeypatch.setenv("AWM_SWARM", "mock")
    assert config.swarm() == "mock"


def test_the_queue_path_defaults_to_the_service_state_path(monkeypatch):
    monkeypatch.delenv("AWM_DOOR_DB")
    assert config.db_path().name == "door.db"
    assert config.db_path().parent.name == "door"
