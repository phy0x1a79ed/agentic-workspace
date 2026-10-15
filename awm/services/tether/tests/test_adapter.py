"""The two things the adapter answers itself, and the one thing it refuses.

Everything else here is a forward, and a forward is only worth testing against
a real daemon — which the Rust suite does. What is worth testing in Python is
the behaviour when there is no daemon to forward to, because that is the state
an operator is in exactly when they need the service to explain itself.
"""

from __future__ import annotations

import asyncio

import pytest

from awm.tether import hub_adapter, paths

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def test_the_verbs_that_must_work_without_a_daemon_are_not_forwards():
    """`status` says why the daemon is missing, `logs` shows it, and `drain`
    says what the session managed to do before it went. Forwarding any of the
    three would make them unanswerable in the one case they exist for."""
    assert hub_adapter.HANDLERS["status"] is hub_adapter.status
    assert hub_adapter.HANDLERS["logs"] is hub_adapter.logs
    assert hub_adapter.HANDLERS["drain"] is hub_adapter.drain


async def test_drain_answers_an_empty_buffer_rather_than_failing():
    """A host that has seen nothing has an answer, not an error."""
    answer = await hub_adapter.drain({}, "nobody-in-particular")
    assert answer["ok"] is True
    assert answer["events"] == []
    assert answer["gap"] is None


async def test_status_answers_with_no_daemon_running(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CONTROL_SOCKET", tmp_path / "absent.sock")
    monkeypatch.setattr(paths, "ROLE", paths.OPERATOR)

    report = await hub_adapter.status({})

    assert report["child"]["role"] == paths.OPERATOR
    assert report["daemon"] == "unavailable"
    assert "logs" in report["hint"]


async def test_status_reports_a_version_lag_when_the_fetch_fell_behind(tmp_path, monkeypatch):
    """A node whose fetch failed runs an older version than RELEASE pins. That
    gap is the whole account of the failure, so `status` has to show it."""
    release = tmp_path / "RELEASE"
    release.write_text("v0.2.0\n")
    version = tmp_path / "bin" / "VERSION"
    version.parent.mkdir()
    version.write_text("v0.1.0\n")
    log = tmp_path / "fetch.log"
    log.write_text("2026-01-01T00:00:00Z tether fetch: could not fetch SHA256SUMS for v0.2.0\n")
    monkeypatch.setattr(paths, "RELEASE_FILE", release)
    monkeypatch.setattr(paths, "VERSION_FILE", version)
    monkeypatch.setattr(paths, "FETCH_LOG", log)
    monkeypatch.setattr(paths, "BIN_OVERRIDE", None)
    monkeypatch.setattr(paths, "CONTROL_SOCKET", tmp_path / "absent.sock")
    monkeypatch.setattr(paths, "ROLE", paths.OPERATOR)

    rel = (await hub_adapter.status({}))["release"]

    assert rel == {
        "pinned": "v0.2.0",
        "installed": "v0.1.0",
        "in_sync": False,
        "last_fetch": "2026-01-01T00:00:00Z tether fetch: could not fetch SHA256SUMS for v0.2.0",
    }


async def test_status_says_a_local_build_is_an_override_not_a_lag(tmp_path, monkeypatch):
    """A developer pointing AWM_TETHER_BIN at their own build runs no fetch, so
    there is no version to compare — say that, rather than a permanent mismatch."""
    monkeypatch.setattr(paths, "BIN_OVERRIDE", "/some/cargo/target")
    monkeypatch.setattr(paths, "CONTROL_SOCKET", tmp_path / "absent.sock")
    monkeypatch.setattr(paths, "ROLE", paths.OPERATOR)

    rel = (await hub_adapter.status({}))["release"]

    assert "in_sync" not in rel
    assert rel["source"] == "AWM_TETHER_BIN=/some/cargo/target"


async def test_logs_reads_the_file_rather_than_the_daemon(tmp_path, monkeypatch):
    log = tmp_path / "daemon.log"
    log.write_text("first\nsecond\nthird\n")
    monkeypatch.setattr(paths, "DAEMON_LOG", log)

    assert (await hub_adapter.logs({"tail": 2}))["lines"] == ["second", "third"]
    # A host whose daemon has never started has no file, and that is not an
    # error — it is the answer.
    monkeypatch.setattr(paths, "DAEMON_LOG", tmp_path / "never.log")
    assert (await hub_adapter.logs({}))["lines"] == []


async def test_startup_schedules_the_child_rather_than_waiting_for_it(monkeypatch):
    """Two failures in one line of code, and both are silent.

    The gateway reaps a service that is slow to become ready, so starting the
    child inline would get this one killed on a slow disk. And the adapter
    awaits whatever ``on_start`` returns if it is awaitable — a Task is — so
    handing back the supervision task would block every inbound call behind a
    loop written never to finish.
    """
    ticks: list[int] = []
    monkeypatch.setattr(hub_adapter.CHILD, "reconcile", lambda: ticks.append(1))
    monkeypatch.setattr(hub_adapter.daemon, "TICK_S", 0.01)

    assert hub_adapter._on_start() is None

    try:
        await asyncio.sleep(0.05)
        assert ticks, "the child was never reconciled"
    finally:
        hub_adapter.SUPERVISION.cancel()
        if hub_adapter.WATCHING is not None:
            hub_adapter.WATCHING.cancel()


async def test_the_relay_host_refuses_the_verbs_that_mint_sessions(monkeypatch):
    """One folder runs on two kinds of host. On the public one a session verb
    is not a failure to reach a daemon — it is the wrong machine, and saying so
    is the difference between a five-second fix and a log dive."""
    monkeypatch.setattr(paths, "ROLE", paths.RELAY)

    answer = await hub_adapter.HANDLERS["invite"]({})

    assert answer["ok"] is False
    assert "operator" in answer["error"]
