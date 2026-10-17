"""Keep every test off this host's real daemon, unit and state."""

from __future__ import annotations

import pytest

from awm.tether import daemon, hub_adapter, paths, stream


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(paths, "STATE_DIR", state)
    monkeypatch.setattr(paths, "CONTROL_SOCKET", state / "operator.sock")
    monkeypatch.setattr(paths, "DAEMON_LOG", state / "daemon.log")
    monkeypatch.setattr(paths, "SESSIONS_DIR", state / "sessions")
    monkeypatch.setattr(paths, "LAUNCH_FILE", state / "daemon.json")
    monkeypatch.setattr(paths, "ENV_FILE", state / "daemon.env")
    monkeypatch.setattr(paths, "STREAM_STATE", state / "stream.json")
    monkeypatch.setattr(paths, "OPERATOR_UNIT", "awm-tether-operator-test")
    monkeypatch.setattr(daemon, "_user_manager_env", lambda: None)
    mark = stream.Mark(state / "stream.json")
    monkeypatch.setattr(hub_adapter, "MARK", mark)
    monkeypatch.setattr(hub_adapter, "SESSION_LOGS", stream.SessionLogs(state / "sessions", mark))
    return state
