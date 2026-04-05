"""Thin stdio MCP proxy for the AWM core.

This module is a minimal bridge: Claude Code (or any MCP client) launches
``awm-mcp`` as a stdio child, and every tool call is forwarded over HTTP to
the long-running ``awm serve`` core (managed by systemd).

Why the split:

- Restarting the core (e.g. after a config/env change) used to drop Claude's
  MCP tools mid-conversation because the stdio child died with the server.
  Now the proxy stays up; it transparently reconnects across core restarts.
- ``handle_tool`` and the MCP tool schemas live in :mod:`awm.tool_dispatch`
  so the old in-process dispatch path and the new HTTP path share one source
  of truth.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import time

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from awm.config import BASE_URL
from awm.tool_dispatch import TOOL_DEFINITIONS

server = Server("awm")


# ---------------------------------------------------------------------------
# MCP protocol handlers
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOL_DEFINITIONS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        result = await _invoke_with_retry(name, arguments)
        return [TextContent(type="text", text=result)]
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

async def _invoke_with_retry(name: str, args: dict, max_wait: float = 10.0) -> str:
    """POST to core /invoke, reconnecting across core restarts.

    When the core is bounced (systemctl restart), the first call sees a
    ``ConnectError``; we nudge systemd and retry for up to ``max_wait`` seconds
    so the user's tool call succeeds transparently.
    """
    deadline = time.monotonic() + max_wait
    last_err: Exception | None = None
    first_attempt = True
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60.0) as client:
        while time.monotonic() < deadline:
            try:
                r = await client.post("/invoke", json={"name": name, "args": args})
                r.raise_for_status()
                return r.json()["result"]
            except httpx.HTTPStatusError:
                raise
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
                last_err = e
                if first_attempt:
                    _ensure_core_running()
                    first_attempt = False
                await asyncio.sleep(0.3)
    raise RuntimeError(f"awm core unreachable after {max_wait}s: {last_err}")


def _ensure_core_running() -> None:
    """Start the core via systemd if it's not already up.

    Best-effort — if systemd isn't available (e.g. the user hasn't enabled
    the unit yet) we fall back to spawning ``awm serve`` as a detached
    subprocess so the proxy still works in dev setups.
    """
    r = subprocess.run(
        ["systemctl", "--user", "start", "awm.service"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        return
    # Fallback: detached subprocess. stdio goes to /dev/null so the proxy
    # doesn't inherit file handles.
    try:
        subprocess.Popen(
            ["awm", "serve"],
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
