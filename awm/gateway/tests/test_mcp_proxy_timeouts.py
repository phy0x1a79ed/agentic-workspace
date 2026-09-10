"""Tests for the MCP proxies' timeout ladder and failure taxonomy.

The bug these pin down was reported twice, months apart, by different callers:
``scope create`` returned ``awm daemon unreachable after 10.0s: timed out``
while the scope was created successfully underneath. Three separate mistakes
produced that one string.

1. The read ceiling was a flat 60s, below the 1800s the scopes manifest declares
   for ``scope_create`` (and the 3600s the dvc service declares), so the client
   gave up while the core was still working.
2. The reconnect window was applied to a reply already owed. A bare
   ``TimeoutError`` from the response phase is an ``OSError``, so the catch-all
   swallowed it into the connect-phase retry loop — which had long since expired,
   hence "10.0s" for a request that waited 60.
3. The core never learns the client left: ``/invoke`` has no disconnect
   awareness and ``ControlChannel.call`` enqueues onto the service's control WS
   before it awaits. So the operation always ran to completion.

The two proxies had also drifted: the SDK one omitted ``httpx.ReadTimeout`` from
its retry tuple entirely, so the same failure escaped its helper and surfaced as
``{"error": ""}`` — which is why nobody connected the two reports to one cause.
"""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytestmark = [pytest.mark.smoke, pytest.mark.mcp]

from awm.gateway import mcp_http


# --------------------------------------------------------------------------
# The ladder
# --------------------------------------------------------------------------

def test_invoke_ceiling_clears_the_largest_declared_budget():
    """The client ceiling is a backstop and must never fire before the server's.

    The largest per-function ``timeout`` any manifest declares is 3600s (the dvc
    service's ``wait`` verb); ``scope_create`` declares 1800s. A ceiling below
    those turns a slow success into a reported failure, which is the whole bug.
    """
    assert mcp_http.DEFAULT_READ_TIMEOUT > 3600.0


def test_catalog_ceiling_stays_short():
    """`GET /tools` blocks session startup and the core answers it in ~19ms."""
    assert mcp_http.DEFAULT_CATALOG_READ_TIMEOUT <= 60.0
    assert mcp_http.DEFAULT_CATALOG_READ_TIMEOUT < mcp_http.DEFAULT_READ_TIMEOUT


def test_reconnect_window_is_separate_from_the_read_ceiling():
    """Different questions: 'is the daemon down?' vs 'is a reply coming?'."""
    assert mcp_http.RECONNECT_WINDOW < mcp_http.DEFAULT_CATALOG_READ_TIMEOUT


def test_overrides_are_read_at_call_time(monkeypatch):
    """`main()` loads the workspace env file AFTER import, so a module-level
    constant would miss it. Same reason ``AWM_AS`` is read per call."""
    monkeypatch.setenv("AWM_MCP_READ_TIMEOUT", "7")
    assert mcp_http.read_timeout() == 7.0
    monkeypatch.setenv("AWM_MCP_READ_TIMEOUT", "nonsense")
    assert mcp_http.read_timeout() == mcp_http.DEFAULT_READ_TIMEOUT
    monkeypatch.setenv("AWM_MCP_READ_TIMEOUT", "0")
    assert mcp_http.read_timeout() == mcp_http.DEFAULT_READ_TIMEOUT


def test_no_reply_envelope_tells_the_caller_not_to_retry():
    """Wording is the fix here: this text reaches the model verbatim."""
    env = mcp_http.no_reply_envelope("scope", "create", 1860.0)
    assert env["error_class"] == "CoreNoReply"
    assert env["tool"] == "scope" and env["verb"] == "create"
    msg = env["error"]
    assert "unreachable" not in msg.lower()
    assert "not a failure report" in msg.lower()
    assert "do not re-issue" in msg.lower()


# --------------------------------------------------------------------------
# Phase classification — a stub core, no gateway
# --------------------------------------------------------------------------

@pytest.fixture()
def silent_core():
    """A core that ACCEPTS the request and never answers — a long scope_create."""
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            time.sleep(30)
        do_GET = do_POST

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture()
def dead_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


class TestStdioProxy:
    def test_response_phase_raises_at_once_and_never_starts_the_core(
            self, silent_core, monkeypatch):
        """Delivered but unanswered: no retry, no systemd nudge.

        Retrying would re-issue a non-idempotent ``/invoke``, and the core is
        demonstrably up — it took the request.
        """
        from awm.gateway import mcp_stdio
        monkeypatch.setenv("AWM_MCP_READ_TIMEOUT", "1")
        monkeypatch.setattr(mcp_stdio.config, "BASE_URL", silent_core)
        started = []
        monkeypatch.setattr(mcp_stdio, "_ensure_core_running",
                            lambda: started.append(1))

        t0 = time.monotonic()
        with pytest.raises(mcp_http.CoreNoReply) as ei:
            mcp_stdio._request_with_retry("POST", "/invoke", {"name": "scope"})
        assert started == [], "a delivered request must not trigger a start"
        assert time.monotonic() - t0 < 5, "must not burn the reconnect window"
        assert ei.value.waited_s >= 1.0

    def test_connect_phase_still_retries_and_still_starts_the_core(
            self, dead_port, monkeypatch):
        """The survives-a-restart property must be untouched."""
        from awm.gateway import mcp_stdio
        monkeypatch.setattr(mcp_stdio.config, "BASE_URL", dead_port)
        started = []
        monkeypatch.setattr(mcp_stdio, "_ensure_core_running",
                            lambda: started.append(1))

        with pytest.raises(mcp_http.CoreUnreachable) as ei:
            mcp_stdio._request_with_retry("POST", "/invoke", {"name": "scope"},
                                          max_wait=1.0)
        assert started == [1], "exactly one start attempt, on the first failure"
        assert "unreachable" in str(ei.value)

    def test_tool_call_surfaces_the_no_reply_envelope(self, silent_core, monkeypatch):
        """The envelope is composed where the tool and verb are in scope."""
        from awm.gateway import mcp_stdio
        monkeypatch.setenv("AWM_MCP_READ_TIMEOUT", "1")
        monkeypatch.setattr(mcp_stdio.config, "BASE_URL", silent_core)
        monkeypatch.setattr(mcp_stdio, "_ensure_core_running", lambda: None)

        res = mcp_stdio._handle_tools_call(
            {"name": "scope", "arguments": {"verb": "create", "project": "p"}})
        env = json.loads(res["content"][0]["text"])
        assert env["error_class"] == "CoreNoReply"
        assert env["tool"] == "scope" and env["verb"] == "create"
        assert env["error"], "never an empty message"


class TestSdkProxyAgrees:
    """The rollback must not behave differently from what it rolls back."""

    def test_both_proxies_share_one_source_of_truth(self):
        from awm.gateway import mcp_server_sdk, mcp_stdio
        assert mcp_stdio.mcp_http is mcp_server_sdk.mcp_http

    def test_response_phase_matches_the_default_proxy(self, silent_core, monkeypatch):
        """`httpx.ReadTimeout` was in neither branch, so it escaped the helper
        and reached callers as ``{"error": ""}``."""
        import asyncio

        from awm.gateway import mcp_server_sdk
        monkeypatch.setenv("AWM_MCP_READ_TIMEOUT", "1")
        monkeypatch.setattr(mcp_server_sdk.config, "BASE_URL", silent_core)
        started = []
        monkeypatch.setattr(mcp_server_sdk, "_ensure_core_running",
                            lambda: started.append(1))

        async def go():
            with pytest.raises(mcp_http.CoreNoReply):
                await mcp_server_sdk._request_with_retry(
                    "POST", "/invoke", {"name": "scope"})

        asyncio.run(go())
        assert started == []

    def test_connect_phase_matches_the_default_proxy(self, dead_port, monkeypatch):
        import asyncio

        from awm.gateway import mcp_server_sdk
        monkeypatch.setattr(mcp_server_sdk.config, "BASE_URL", dead_port)
        started = []
        monkeypatch.setattr(mcp_server_sdk, "_ensure_core_running",
                            lambda: started.append(1))

        async def go():
            with pytest.raises(mcp_http.CoreUnreachable):
                await mcp_server_sdk._request_with_retry(
                    "POST", "/invoke", {"name": "scope"}, max_wait=1.0)

        asyncio.run(go())
        assert started == [1]


@pytest.mark.subprocess
@pytest.mark.slow
def test_proxy_exits_cleanly_when_stdin_closes_mid_call(silent_core, tmp_path):
    """A client that disappears mid-call must not abort the proxy.

    ``main()`` returns on stdin EOF and every dispatch thread is a daemon, so
    interpreter finalization races a thread blocked on a reply — it dies with
    ``_enter_buffered_busy: could not acquire lock`` and a core dump (SIGABRT,
    exit 134). Always possible; the long read ceiling makes it likely, because
    a call can now legitimately be in flight for an hour rather than a minute.
    """
    import os
    import subprocess
    import sys as _sys
    from pathlib import Path

    gw = Path(__file__).resolve().parents[1]           # awm/gateway
    comps = gw.parent / "service_components"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([
        str(gw), str(comps / "config"), str(comps / "persistence"),
        str(comps / "gatewayclient"),
    ])
    env["AWM_PORT"] = silent_core.rsplit(":", 1)[1]
    env["AWM_MCP_READ_TIMEOUT"] = "30"          # longer than we wait below

    msgs = (
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":'
        '{"protocolVersion":"2024-11-05","capabilities":{},'
        '"clientInfo":{"name":"t","version":"1"}}}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":'
        '{"name":"scope","arguments":{"verb":"create"}}}\n'
    )
    # stdin closes immediately, while the tool call is still waiting on a reply.
    proc = subprocess.run(
        [_sys.executable, "-m", "awm.gateway.mcp_server"],
        input=msgs, text=True, env=env, capture_output=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"expected a clean exit, got {proc.returncode}: {proc.stderr[-400:]}")
    assert "could not acquire lock" not in proc.stderr
