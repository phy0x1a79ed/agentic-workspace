"""Thin stdio MCP proxy for the AWM core.

This module is a minimal bridge: Claude Code (or any MCP client) launches
``awm-mcp`` as a stdio child, and every request is forwarded over HTTP to
the long-running ``awm serve`` core (managed by systemd).

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
import time
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from awm import config
from awm.services import auth as auth_svc

server = Server("awm")


def _base_url() -> str:
    """HTTPS base URL of the local awm daemon (the exposed listener)."""
    return auth_svc.base_url()


def _client_kwargs() -> dict:
    """Auth + TLS kwargs for outbound httpx calls."""
    return auth_svc.client_kwargs(timeout=60.0)


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
    """
    data = await _request_with_retry("GET", "/tools")
    return [Tool.model_validate(t) for t in data["tools"]]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        data = await _request_with_retry(
            "POST", "/invoke", json_body={"name": name, "args": arguments}
        )
        return [TextContent(type="text", text=data["result"])]
    except httpx.HTTPStatusError as e:
        # Core returned a structured error — surface its body.
        try:
            body = e.response.json()
            detail = body.get("detail", str(e))
        except Exception:
            detail = e.response.text or str(e)
        return [TextContent(type="text", text=json.dumps({"error": detail}))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


# ---------------------------------------------------------------------------
# HTTP dispatch with reconnect
# ---------------------------------------------------------------------------

async def _request_with_retry(
    method: str,
    path: str,
    json_body: dict | None = None,
    max_wait: float = 10.0,
) -> dict[str, Any]:
    """Make an HTTPS request to the local awm daemon, reconnecting across
    restarts.

    Auth and TLS verification kwargs come from
    :func:`awm.services.auth.client_kwargs`. On a transport error the first
    attempt nudges systemd to start the daemon, then we retry for up to
    ``max_wait`` seconds so the caller's request transparently survives a
    ``systemctl restart``.
    """
    deadline = time.monotonic() + max_wait
    last_err: Exception | None = None
    first_attempt = True
    kwargs = _client_kwargs()
    async with httpx.AsyncClient(base_url=_base_url(), **kwargs) as client:
        while time.monotonic() < deadline:
            try:
                r = await client.request(method, path, json=json_body)
                r.raise_for_status()
                return r.json()
            except httpx.HTTPStatusError:
                raise
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
                last_err = e
                if first_attempt:
                    _ensure_core_running()
                    first_attempt = False
                await asyncio.sleep(0.3)
    raise RuntimeError(f"awm daemon unreachable after {max_wait}s: {last_err}")


def _ensure_core_running() -> None:
    """Start the awm daemon via systemd if it's not already up.

    Starts both ``awm.service`` (core, in-process state) and
    ``awm-exposed.service`` (HTTPS listener) so the MCP proxy can reach
    the catalog endpoints. Best-effort — falls back to detached
    ``awm serve-exposed`` in dev setups without systemd.
    """
    r = subprocess.run(
        ["systemctl", "--user", "start",
         "awm.service", "awm-exposed.service"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        return
    # Fallback: detached subprocess. stdio goes to /dev/null so the proxy
    # doesn't inherit file handles.
    try:
        subprocess.Popen(
            ["awm", "serve-exposed"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        pass  # no `awm` on PATH — propagate connection error to caller


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
        await server.run(read, write, server.create_initialization_options())


def main():
    _reap_orphan_clients()
    _ensure_core_running()
    asyncio.run(_run())


if __name__ == "__main__":
    main()
