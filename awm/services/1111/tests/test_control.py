"""Tests for the sudo-bridge control layer and the manifest/handler shape.

Runs without any real `u1111` account or live process — `subprocess.run` is
mocked, so these pin two things: the exact sudo invocation shape (no wildcard
argument, `-n` so a missing NOPASSWD rule fails loudly instead of hanging on a
password prompt), and that every manifest tool projects as `1111_<verb>`.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from awm.svc1111 import control, hub_adapter, register


def _fake_run(returncode=0, stdout="", stderr=""):
    def _inner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
    return _inner


def test_start_invokes_exact_sudo_bridge_command():
    with patch("subprocess.run", side_effect=_fake_run(0, "started pid=123")) as m:
        result = control.start()
    (cmd,), kwargs = m.call_args
    assert cmd == ["sudo", "-n", "-u", "u1111", "/home/u1111/bin/1111ctl.sh", "start"]
    assert result["rc"] == 0
    assert "started" in result["stdout"]


def test_status_sets_running_from_rc():
    with patch("subprocess.run", side_effect=_fake_run(0, "running pid=1")):
        assert control.status()["running"] is True
    with patch("subprocess.run", side_effect=_fake_run(1, "stopped")):
        assert control.status()["running"] is False


def test_timeout_reported_not_raised():
    def _raise(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))
    with patch("subprocess.run", side_effect=_raise):
        result = control.stop()
    assert result["rc"] == -1
    assert "timed out" in result["error"]


def test_manifest_tools_are_1111_prefixed():
    tools = [f["tool"] for f in hub_adapter.API_MANIFEST["functions"]]
    assert tools == ["1111_start", "1111_stop", "1111_restart", "1111_status"]


def test_handlers_match_manifest_functions():
    names = {f["name"] for f in hub_adapter.API_MANIFEST["functions"]}
    assert names == set(hub_adapter.HANDLERS.keys())


def test_register_posts_url_only_payload_no_page_fields(monkeypatch):
    # The registration payload is `url`-only — no `static`/`dir`, which is
    # what actually keeps this out of any `kind=page` listing.
    import asyncio

    monkeypatch.setenv("AWM_HUB_URL", "http://127.0.0.1:7819")
    posted = {}

    class _FakeResponse:
        status_code = 400  # short-circuit before the websocket dance

        def json(self):
            return {}

        @property
        def text(self):
            return "boom"

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            posted["url"] = url
            posted["json"] = json
            return _FakeResponse()

    monkeypatch.setattr(register.httpx, "AsyncClient", lambda **kw: _FakeClient())

    with pytest.raises(RuntimeError, match="register failed"):
        asyncio.run(register.hold_registration())

    assert posted["url"].endswith("/hub/register")
    assert posted["json"] == {
        "name": "1111",
        "prefix": "/1111",
        "url": f"http://127.0.0.1:{register.WEBUI_PORT}",
        "strip_prefix": False,
    }
    assert "static" not in posted["json"]
    assert "dir" not in posted["json"]


def test_register_reads_hub_url_from_env(monkeypatch):
    monkeypatch.delenv("AWM_HUB_URL", raising=False)
    import asyncio
    with pytest.raises(RuntimeError, match="AWM_HUB_URL"):
        asyncio.run(register.hold_registration())
