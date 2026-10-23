"""The enable gate: the front door runs only on a fleet node with AWM_FRONT_DOOR=1."""

from __future__ import annotations

import logging

import pytest

from awm.reporting import config, hub_adapter, reconcile

from stubs import FakeCx

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


async def test_start_on_a_station_logs_a_refusal_and_builds_nothing(monkeypatch, caplog, tmp_path):
    monkeypatch.setenv("AWM_NODE_ROLE", "station")
    monkeypatch.setattr(hub_adapter, "RUNTIME", None)
    spawned = []
    monkeypatch.setattr(hub_adapter, "spawn_supervised", lambda *a, **k: spawned.append(a))
    with caplog.at_level(logging.WARNING):
        await hub_adapter._on_start()
    assert hub_adapter.RUNTIME is None
    assert spawned == []
    assert not (tmp_path / "reporting.db").exists()
    assert "not running" in caplog.text


async def test_start_with_the_flag_off_runs_no_front_door_tasks(monkeypatch):
    monkeypatch.setenv("AWM_FRONT_DOOR", "0")
    monkeypatch.setattr(hub_adapter, "RUNTIME", None)
    spawned = []
    monkeypatch.setattr(hub_adapter, "spawn_supervised", lambda name, *a: spawned.append(name))
    await hub_adapter._on_start()
    assert spawned == ["reporting-reconcile", "reporting-journals"]
    assert hub_adapter.RUNTIME is not None and not hub_adapter.RUNTIME.door


async def test_start_with_the_flag_on_follows_the_board_and_the_commands(monkeypatch):
    monkeypatch.setenv("AWM_BOARD_URL", "http://board.example")
    monkeypatch.setenv("AWM_BOARD_TOKEN", "tok")
    monkeypatch.setattr(hub_adapter, "RUNTIME", None)
    spawned = []
    monkeypatch.setattr(hub_adapter, "spawn_supervised", lambda name, *a: spawned.append(name))
    await hub_adapter._on_start()
    assert spawned == ["reporting-reconcile", "reporting-journals", "reporting-commands", "reporting-approvals",
                       "reporting-subscribe"]
    assert hub_adapter.RUNTIME.door


async def test_the_sweep_ticker_sweeps_nothing_on_a_station(monkeypatch):
    monkeypatch.setenv("AWM_NODE_ROLE", "station")
    seen = []

    async def on_rows(rows):
        seen.append(rows)

    await reconcile.Loop(FakeCx([{"job": "j"}]), on_rows=on_rows).tick()
    assert seen == []


async def test_the_sweep_ticker_runs_on_a_node_with_the_flag_off(monkeypatch):
    monkeypatch.setenv("AWM_FRONT_DOOR", "0")
    seen = []

    async def on_rows(rows):
        seen.append(rows)

    await reconcile.Loop(FakeCx([{"job": "j"}]), on_rows=on_rows).tick()
    assert seen == [[{"job": "j"}]]


def test_the_swarm_comes_from_the_node_and_not_a_constant(monkeypatch):
    assert config.swarm() == "tony"
    monkeypatch.setenv("AWM_SWARM", "mock")
    assert config.swarm() == "mock"


def test_the_queue_path_defaults_to_the_service_state_path(monkeypatch):
    monkeypatch.delenv("AWM_REPORTING_DB")
    assert config.db_path().name == "reporting.db"
    assert config.db_path().parent.name == "reporting"
