"""Thin stdio MCP proxy for the AWM core.

This module is a minimal bridge: Claude Code (or any MCP client) launches
``awm-mcp`` as a stdio child, and every request is forwarded over HTTP to
the long-running ``awm gateway serve`` core (managed by systemd).

Design invariant — the proxy is **stateless**. It holds no application data
(tool definitions, schemas, models, service objects). The only things imported
from the ``awm`` package are static configuration constants. Tool metadata is
fetched fresh from the core on every ``list_tools`` call, so adding or
removing a tool and restarting only the core is enough — the proxy picks up
the new surface on the next request without needing Claude Code to restart.

Why the split:

- Restarting the core (e.g. after a config/env change) used to drop Claude's
  MCP tools mid-conversation because the stdio child died with the server.
  Now the proxy stays up; it transparently reconnects across core restarts.
- Keeping the proxy stateless means core restarts fully take effect — no
  stale snapshots bound at proxy launch time.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from typing import Any

import httpx
from mcp.server import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from awm import config
from awm.gateway import mcp_caller, mcp_http
from awm.gateway._path import resolve_bin

server = Server("awm")
_watch = mcp_http.ToolListWatch()
_watcher: asyncio.Task | None = None


# ---------------------------------------------------------------------------
# MCP protocol handlers
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[Tool]:
    """Fetch the tool surface from the core on every call — no local cache.

    If the core is unreachable after the retry deadline, the error propagates
    up to the MCP client. Deliberately no fallback to a bundled snapshot: a
    stale list is worse than an honest error because it silently drifts from
    the live tool surface after a core restart.

    Requests the collapsed per-domain surface (``?view=domains``): one generic
    ``{verb, args, peer}`` tool per domain instead of hundreds of verb-tools, so a
    non-deferring client (a spawned bot, opencode) carries a tiny tool
    surface and learns each domain's verbs + params on demand via the reserved
    ``describe`` verb. The CLI/HTTP surfaces stay expanded (they read the default
    ``/tools``).

    ``peers=1`` makes that surface **fleet-wide** without duplicating it: one tool
    per domain name whatever the peer count, each knowing which node it defaults
    to. This proxy used to fan out to every
    peer's edge here and merge their whole catalogs under ``<domain>@<peer>``
    names — two peers tripled the tool list for no new capability, and every
    single ``list_tools`` paid an ssh per peer. The gateway now keeps that map in a
    background snapshot, so this is one loopback GET.

    Every tool is listed top-level; a tool switched off by its service's tier is
    absent.
    """
    global _watcher
    data = await _fetch_tools()
    _watch.served(data["tools"])
    if _watcher is None:
        # A list request only arrives after the client's ``initialized``, so the
        # session may notify from here on.
        _watcher = asyncio.create_task(
            _watch_tool_list(server.request_context.session))
    return [Tool.model_validate(t) for t in data["tools"]]


async def _fetch_tools(**kwargs) -> dict[str, Any]:
    return await _request_with_retry(
        "GET", "/tools", params={"view": "domains", "peers": "1"},
        read_timeout=mcp_http.catalog_read_timeout(), **kwargs)


async def _watch_tool_list(session) -> None:
    """Tell the client when the tool list it was served goes stale."""
    started = time.monotonic()
    while True:
        await asyncio.sleep(mcp_http.watch_delay(time.monotonic() - started))
        try:
            tools = (await _fetch_tools(max_wait=1.0, start_core=False))["tools"]
        except Exception:  # noqa: BLE001 — a core that is down changes nothing
            continue
        if _watch.changed(tools):
            await session.send_tool_list_changed()


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        # A placed agent's proxy carries its placement identity in AWM_AS (set
        # in its per-placement spawn-mcp config). Stamp it as X-Awm-As so the
        # core can resolve the call to that placement (the placement B-op tools
        # need no model-supplied token). Read at call time, not import time, so a
        # reused proxy always reflects its own env. Absent for normal sessions.
        as_ = os.environ.get("AWM_AS")
        # This proxy is spawned by the calling session as a stdio child — one
        # `claude` REPL, one `awm-mcp` child — so our parent pid is normally the
        # caller. That is the identity the reflection service targets, and it is
        # the same fact whether the session sits in a tmux pane or runs as a
        # background job, which is what lets an agent reflect on itself without
        # knowing how it happens to be hosted. Read at call time (see AWM_AS
        # above); it is observed here rather than accepted as an argument, so a
        # model cannot aim reflection at anyone but itself. A wrapper in the
        # configured command breaks the one-hop assumption, so walk up to the
        # nearest ancestor that is a session (see mcp_caller).
        session_pid = str(mcp_caller.resolve_caller_pid(os.getppid()))
        headers = {}
        if as_:
            headers["X-Awm-As"] = as_
        if session_pid:
            headers["X-Awm-Session-Pid"] = session_pid
        arguments = mcp_http.with_search_context(name, arguments)
        data = await _request_with_retry(
            "POST", "/invoke", json_body={"name": name, "args": arguments},
            headers=headers,
        )
        return [TextContent(type="text", text=data["result"])]
    except httpx.HTTPStatusError as e:
        # Core returned a structured error — surface its body.
        try:
            body = e.response.json()
            detail = body.get("detail", str(e))
        except Exception:
            detail = e.response.text or str(e)
        # If detail is a structured {error_class, error, ...} dict (from
        # /invoke's catch-all), surface those fields directly so the MCP
        # caller sees the real exception class instead of a bare 500.
        if isinstance(detail, dict):
            return [TextContent(type="text", text=json.dumps(detail))]
        return [TextContent(type="text", text=json.dumps({"error": detail}))]
    except mcp_http.CoreNoReply as e:
        # Delivered but unanswered. Composed here because this is where the tool
        # and verb are known; the helper only knows it waited.
        env = mcp_http.no_reply_envelope(name, arguments.get("verb"), e.waited_s)
        return [TextContent(type="text", text=json.dumps(env))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


# ---------------------------------------------------------------------------
# HTTP dispatch with reconnect
# ---------------------------------------------------------------------------

async def _request_with_retry(
    method: str,
    path: str,
    json_body: dict | None = None,
    max_wait: float = mcp_http.RECONNECT_WINDOW,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    read_timeout: float | None = None,
    start_core: bool = True,
) -> dict[str, Any]:
    """Make an HTTP request to the local awm daemon, reconnecting across
    restarts.

    The phase split of :mod:`awm.gateway.mcp_stdio` — see
    :mod:`awm.gateway.mcp_http` for why the reconnect window and the read
    ceiling are different questions. httpx names the phases directly, so the
    classification is explicit rather than inherited from exception ancestry.

    This is the rollback proxy and it had drifted: ``httpx.ReadTimeout`` was in
    neither branch, so an unanswered request escaped this helper entirely and
    reached the caller as ``{"error": ""}`` — httpx gives a ReadTimeout an empty
    message. Same bug as the default proxy's, presenting with no message at all.
    """
    if read_timeout is None:
        read_timeout = mcp_http.read_timeout()
    started = time.monotonic()
    deadline = started + max_wait
    last_err: Exception | None = None
    first_attempt = True
    tmo = httpx.Timeout(read_timeout, connect=10.0)
    async with httpx.AsyncClient(base_url=config.BASE_URL, timeout=tmo) as client:
        while time.monotonic() < deadline:
            try:
                r = await client.request(method, path, json=json_body,
                                         headers=headers, params=params)
                r.raise_for_status()
                return r.json()
            except httpx.HTTPStatusError:
                raise
            except (httpx.ReadTimeout, httpx.WriteTimeout) as e:
                # Response phase: delivered (or mid-delivery), unanswered. Never
                # retried — see the non-idempotency argument in mcp_http. A
                # WriteTimeout on a small loopback body is near-impossible, and
                # is grouped here deliberately: a half-sent request that MIGHT
                # have landed is safer to re-check than to replay.
                raise mcp_http.CoreNoReply(
                    time.monotonic() - started, path) from e
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,
                    httpx.ReadError, httpx.RemoteProtocolError) as e:
                # Connect phase — PoolTimeout included: no connection was ever
                # handed out, so nothing was sent and replay is safe.
                last_err = e
                if first_attempt and start_core:
                    _ensure_core_running()
                first_attempt = False
                await asyncio.sleep(0.3)
    raise mcp_http.CoreUnreachable(
        f"awm daemon unreachable after {max_wait}s: {last_err}")


def _ensure_core_running() -> None:
    """Start the awm daemon via systemd if it's not already up.

    Best-effort — falls back to detached ``awm gateway serve`` in dev setups
    without systemd. Port-checks before spawning the fallback to avoid
    racing into a zombie binding the listener.
    """
    r = subprocess.run(
        ["systemctl", "--user", "start", "awm.service"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        return
    # Port-check: if something is already listening on the configured port, don't
    # spawn a duplicate. (The status loop above will recover when the existing
    # process becomes responsive.)
    import socket as _socket
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        try:
            s.connect(("127.0.0.1", config.PORT))
            return
        except OSError:
            pass
    from awm.gateway._process_utils import system_unit_owns_gateway  # noqa: PLC0415
    if system_unit_owns_gateway():
        return
    subprocess.Popen(
        [resolve_bin("awm"), "gateway", "serve"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# ---------------------------------------------------------------------------
# Orphan reaper
# ---------------------------------------------------------------------------

def _reap_orphan_clients() -> None:
    """SIGTERM sibling awm-mcp processes whose parent PID no longer exists.

    Safe: only kills parentless orphans (parent PID = 1 means the original
    parent died), never live Claude sessions. Leftovers accumulate when Claude
    Code is killed without giving MCP children time to clean up.
    """
    try:
        mypid = os.getpid()
        out = subprocess.run(
            ["pgrep", "-f", "awm-mcp"],
            capture_output=True, text=True,
        )
        if out.returncode != 0:
            return
        for line in out.stdout.splitlines():
            pid_s = line.strip()
            if not pid_s.isdigit():
                continue
            pid = int(pid_s)
            if pid == mypid:
                continue
            try:
                with open(f"/proc/{pid}/stat") as f:
                    fields = f.read().split()
                ppid = int(fields[3])
            except (FileNotFoundError, IndexError, ValueError):
                continue
            if ppid == 1:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
    except Exception:
        # Reaping is best-effort — never block startup on it.
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _run():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options(
            NotificationOptions(tools_changed=True)))


def main():
    # Per-workspace env file: merge $AWM_WORKSPACE/.awm/env into os.environ
    # so the fallback-spawned core inherits it (inbox #236).
    from awm.config import load_env_file
    load_env_file()
    _reap_orphan_clients()
    _ensure_core_running()
    asyncio.run(_run())


if __name__ == "__main__":
    main()
