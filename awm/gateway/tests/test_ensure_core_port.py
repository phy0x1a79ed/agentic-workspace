"""The stdio proxy looks for the core on the configured port, not a literal 7819."""

from __future__ import annotations

import socket
import subprocess
from types import SimpleNamespace

from awm.gateway import mcp_stdio


class _Socket:
    """Connects only if the port is in `listening`; records every attempt."""

    attempts: list = []
    listening: set = set()

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def connect(self, addr):
        type(self).attempts.append(addr)
        if addr[1] not in type(self).listening:
            raise ConnectionRefusedError


def _arrange(monkeypatch, port, listening):
    _Socket.attempts, _Socket.listening = [], set(listening)
    monkeypatch.setattr(socket, "socket", _Socket)
    monkeypatch.setattr(mcp_stdio.config, "PORT", port)
    spawned = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: spawned.append(a) or SimpleNamespace(returncode=1))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: spawned.append(a))
    return spawned


def test_a_core_on_the_configured_port_is_found_and_nothing_is_started(monkeypatch):
    spawned = _arrange(monkeypatch, 9999, {9999})
    mcp_stdio._ensure_core_running()
    assert _Socket.attempts == [("127.0.0.1", 9999)]
    assert spawned == []


def test_the_default_port_is_not_consulted_when_another_is_configured(monkeypatch):
    spawned = _arrange(monkeypatch, 9999, {7819})
    monkeypatch.setattr("awm.gateway._path.resolve_bin", lambda name: name)
    mcp_stdio._ensure_core_running()
    assert {a[1] for a in _Socket.attempts} == {9999}
    assert spawned, "a core that is not on the configured port must be started"


def test_the_post_systemd_recheck_uses_the_configured_port(monkeypatch):
    _arrange(monkeypatch, 9999, {9999})
    _Socket.listening = set()
    attempts = []

    class _Late(_Socket):
        def connect(self, addr):
            attempts.append(addr)
            if len(attempts) >= 2:
                return
            raise ConnectionRefusedError

    monkeypatch.setattr(socket, "socket", _Late)
    mcp_stdio._ensure_core_running()
    assert attempts == [("127.0.0.1", 9999), ("127.0.0.1", 9999)]
