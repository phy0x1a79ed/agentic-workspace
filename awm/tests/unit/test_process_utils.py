"""Unit tests for awm.services._process_utils (probe + orphan sweep)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

pytestmark = [pytest.mark.unit]

from awm.services import _process_utils as pu


# ---------------------------------------------------------------------------
# probe_existing_awm
# ---------------------------------------------------------------------------


def _fake_socket_refusing():
    """Return a socket-like context manager whose connect() raises."""
    s = MagicMock()
    s.connect.side_effect = ConnectionRefusedError("refused")
    return s


def _fake_socket_connects():
    s = MagicMock()
    s.connect.return_value = None
    return s


def test_probe_free_when_port_unbound(monkeypatch):
    monkeypatch.setattr(pu.socket, "socket", lambda *a, **kw: _fake_socket_refusing())
    result = pu.probe_existing_awm("127.0.0.1", 7819, "/some/workspace")
    assert result.state == "free"
    assert result.holder_pid is None


def _mock_urlopen(payload: dict, status: int = 200):
    """Return a urllib.request.urlopen replacement that yields ``payload``."""
    body = json.dumps(payload).encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = body
    resp.status = status
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return MagicMock(return_value=resp)


def test_probe_healthy_peer_same_workspace(monkeypatch):
    monkeypatch.setattr(pu.socket, "socket", lambda *a, **kw: _fake_socket_connects())
    monkeypatch.setattr(
        pu.urllib.request, "urlopen",
        _mock_urlopen({"status": "ok", "workspace_root": "/ws", "active_scopes": 5}),
    )
    result = pu.probe_existing_awm("127.0.0.1", 7819, "/ws")
    assert result.state == "healthy_peer"
    assert result.detail == "/ws"


def test_probe_foreign_holder_wrong_workspace(monkeypatch):
    monkeypatch.setattr(pu.socket, "socket", lambda *a, **kw: _fake_socket_connects())
    monkeypatch.setattr(
        pu.urllib.request, "urlopen",
        _mock_urlopen({"status": "ok", "workspace_root": "/elsewhere", "active_scopes": 5}),
    )
    monkeypatch.setattr(pu, "_find_port_holder", lambda h, p: (12345, "awm serve"))
    result = pu.probe_existing_awm("127.0.0.1", 7819, "/ws")
    assert result.state == "foreign_holder"
    assert result.holder_pid == 12345
    assert "workspace_root" in (result.detail or "")


def test_probe_foreign_holder_garbage_response(monkeypatch):
    monkeypatch.setattr(pu.socket, "socket", lambda *a, **kw: _fake_socket_connects())
    body = b"not json at all"
    resp = MagicMock()
    resp.read.return_value = body
    resp.status = 200
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    monkeypatch.setattr(pu.urllib.request, "urlopen", MagicMock(return_value=resp))
    monkeypatch.setattr(pu, "_find_port_holder", lambda h, p: (777, "nginx"))
    result = pu.probe_existing_awm("127.0.0.1", 7819, "/ws")
    assert result.state == "foreign_holder"
    assert result.holder_pid == 777


def test_probe_foreign_holder_missing_fields(monkeypatch):
    monkeypatch.setattr(pu.socket, "socket", lambda *a, **kw: _fake_socket_connects())
    monkeypatch.setattr(
        pu.urllib.request, "urlopen",
        _mock_urlopen({"status": "ok"}),
    )
    monkeypatch.setattr(pu, "_find_port_holder", lambda h, p: (None, None))
    result = pu.probe_existing_awm("127.0.0.1", 7819, "/ws")
    assert result.state == "foreign_holder"
    assert "missing expected fields" in (result.detail or "")


def test_exit_if_healthy_peer_exits_zero(monkeypatch):
    monkeypatch.setattr(
        pu, "probe_existing_awm",
        lambda *a, **kw: pu.ProbeResult("healthy_peer", None, None, "/ws"),
    )
    with pytest.raises(SystemExit) as ei:
        pu.exit_if_healthy_peer("127.0.0.1", 7819, "/ws")
    assert ei.value.code == 0


def test_exit_if_healthy_peer_exits_one_on_foreign(monkeypatch):
    monkeypatch.setattr(
        pu, "probe_existing_awm",
        lambda *a, **kw: pu.ProbeResult("foreign_holder", 99, "stranger", "bad"),
    )
    with pytest.raises(SystemExit) as ei:
        pu.exit_if_healthy_peer("127.0.0.1", 7819, "/ws")
    assert ei.value.code == 1


def test_exit_if_healthy_peer_returns_on_free(monkeypatch):
    monkeypatch.setattr(
        pu, "probe_existing_awm",
        lambda *a, **kw: pu.ProbeResult("free", None, None, None),
    )
    # Should NOT raise — returns silently.
    pu.exit_if_healthy_peer("127.0.0.1", 7819, "/ws")


# ---------------------------------------------------------------------------
# sweep_orphan_awm_serves
# ---------------------------------------------------------------------------


def test_is_awm_serve_cmdline_matches_serve():
    assert pu._is_awm_serve_cmdline("/home/x/awm serve")
    assert pu._is_awm_serve_cmdline("awm serve --port 7819")
    assert pu._is_awm_serve_cmdline("python /usr/bin/awm serve")


def test_is_awm_serve_cmdline_rejects_mcp():
    # awm-mcp must not be confused with `awm serve`.
    assert not pu._is_awm_serve_cmdline("/home/x/awm-mcp")
    assert not pu._is_awm_serve_cmdline("awm status")
    assert not pu._is_awm_serve_cmdline("awmctl serve")


def _run_factory(by_cmd):
    """Build a fake subprocess.run that dispatches by argv[0]/argv[1]."""
    def fake_run(args, **kw):
        key = " ".join(args[:3])
        return by_cmd.get(key, by_cmd.get(args[0], MagicMock(returncode=1, stdout="")))
    return fake_run


def test_sweep_no_processes(monkeypatch):
    monkeypatch.setattr(
        pu.subprocess, "run",
        _run_factory({"pgrep -f awm serve": MagicMock(returncode=1, stdout="")}),
    )
    reports = pu.sweep_orphan_awm_serves(self_pid=1)
    assert reports == []


def test_sweep_skips_in_cgroup(monkeypatch):
    monkeypatch.setattr(
        pu.subprocess, "run",
        _run_factory({
            "pgrep -f awm serve": MagicMock(returncode=0, stdout="100\n"),
            "systemctl --user show": MagicMock(returncode=0, stdout="MainPID=100\n"),
        }),
    )
    monkeypatch.setattr(pu, "_read_cmdline", lambda pid: "/usr/bin/awm serve")
    monkeypatch.setattr(pu, "_is_in_awm_service_cgroup", lambda pid: True)
    kills = []
    monkeypatch.setattr(pu.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    reports = pu.sweep_orphan_awm_serves(self_pid=9999)
    assert kills == []
    assert any(r.action.startswith("skipped:in_cgroup") for r in reports)


def test_sweep_kills_orphan(monkeypatch):
    monkeypatch.setattr(
        pu.subprocess, "run",
        _run_factory({
            "pgrep -f awm serve": MagicMock(returncode=0, stdout="264\n"),
            "systemctl --user show": MagicMock(returncode=0, stdout="MainPID=54311\n"),
        }),
    )
    monkeypatch.setattr(pu, "_read_cmdline", lambda pid: "/usr/bin/awm serve")
    monkeypatch.setattr(pu, "_is_in_awm_service_cgroup", lambda pid: False)
    monkeypatch.setattr(pu, "_pid_alive", lambda pid: False)
    kills = []
    monkeypatch.setattr(pu.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    reports = pu.sweep_orphan_awm_serves(self_pid=9999)
    assert any(pid == 264 for pid, sig in kills)
    assert any(r.action == "killed" for r in reports)


def test_sweep_skips_self(monkeypatch):
    monkeypatch.setattr(
        pu.subprocess, "run",
        _run_factory({
            "pgrep -f awm serve": MagicMock(returncode=0, stdout="42\n"),
            "systemctl --user show": MagicMock(returncode=0, stdout="MainPID=99\n"),
        }),
    )
    monkeypatch.setattr(pu, "_read_cmdline", lambda pid: "/usr/bin/awm serve")
    kills = []
    monkeypatch.setattr(pu.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    reports = pu.sweep_orphan_awm_serves(self_pid=42)
    assert kills == []
    assert any(r.action == "skipped:self" for r in reports)


def test_sweep_skips_non_awm_serve_match(monkeypatch):
    # pgrep -f matches substring; an `awm-mcp` proxy must be skipped, not killed.
    monkeypatch.setattr(
        pu.subprocess, "run",
        _run_factory({
            "pgrep -f awm serve": MagicMock(returncode=0, stdout="555\n"),
            "systemctl --user show": MagicMock(returncode=0, stdout="MainPID=99\n"),
        }),
    )
    monkeypatch.setattr(pu, "_read_cmdline", lambda pid: "/usr/bin/awm-mcp")
    kills = []
    monkeypatch.setattr(pu.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    reports = pu.sweep_orphan_awm_serves(self_pid=9999)
    assert kills == []
    assert any(r.action == "skipped:not_awm_serve" for r in reports)
