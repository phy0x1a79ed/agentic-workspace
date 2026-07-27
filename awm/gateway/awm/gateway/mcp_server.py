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
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from awm import config
from awm.gateway._path import resolve_bin

server = Server("awm")


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
    ``{verb, args}`` tool per domain instead of ~71 verb-tools, so a
    non-deferring client (a spawned agentcore bot, opencode) carries a tiny tool
    surface and learns each domain's verbs + params on demand via the reserved
    ``describe`` verb. The CLI/HTTP surfaces stay expanded (they read the default
    ``/tools``).
    """
    data = await _request_with_retry("GET", "/tools", params={"view": "domains"})
    tools = [Tool.model_validate(t) for t in data["tools"]]
    # Client-side federation: merge each registered peer's collapsed catalog,
    # namespaced ``<domain>@<peer>``, by reading the peer's edge DIRECTLY (never
    # relayed through this gateway). Best-effort — a down/unreachable peer simply
    # contributes no tools and never blocks the local surface.
    peers = await _list_peers()
    if peers:
        results = await asyncio.gather(
            *(_peer_tools(p) for p in peers), return_exceptions=True)
        for r in results:
            if isinstance(r, list):
                tools.extend(r)
    return tools


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        # A placed agent's proxy carries its placement identity in AWM_AS (set
        # in its per-placement spawn-mcp config). Stamp it as X-Awm-As so the
        # core can resolve the call to that placement (the agents B-op tools
        # need no model-supplied token). Read at call time, not import time, so a
        # reused proxy always reflects its own env. Absent for normal sessions.
        as_ = os.environ.get("AWM_AS")
        # A ``$TMUX_PANE``-hosted session's proxy sits inside that exact pane's
        # environment (one `claude` process, one `awm-mcp` child), so it can
        # forward the pane id the same way it forwards AWM_AS — letting the
        # reflection service target the caller's own pane with zero pid/pane
        # awareness required from the agent. Absent outside tmux (IDE
        # extension, web, SDK-driven sessions). Read at call time, same reason
        # as AWM_AS above.
        tmux_pane = os.environ.get("TMUX_PANE")
        # A ``<domain>@<peer>`` tool name routes to that peer's edge directly.
        if "@" in name:
            base, _, peer_name = name.rpartition("@")
            data = await _peer_invoke(peer_name, base, arguments, as_)
            return [TextContent(type="text", text=data["result"])]
        headers = {}
        if as_:
            headers["X-Awm-As"] = as_
        if tmux_pane:
            headers["X-Awm-Tmux-Pane"] = tmux_pane
        data = await _request_with_retry(
            "POST", "/invoke", json_body={"name": name, "args": arguments},
            headers=headers or None,
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
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Make an HTTP request to the local awm daemon, reconnecting across
    restarts.

    On a transport error the first attempt nudges systemd to start the
    daemon, then we retry for up to ``max_wait`` seconds so the caller's
    request transparently survives a ``systemctl restart``.
    """
    deadline = time.monotonic() + max_wait
    last_err: Exception | None = None
    first_attempt = True
    async with httpx.AsyncClient(base_url=config.BASE_URL, timeout=60.0) as client:
        while time.monotonic() < deadline:
            try:
                r = await client.request(method, path, json=json_body,
                                         headers=headers, params=params)
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


# ---------------------------------------------------------------------------
# Cross-peer federation (client-side, no relay)
# ---------------------------------------------------------------------------

async def _list_peers() -> list[dict[str, Any]]:
    """The local gateway's peer book. Empty on any error (federation optional)."""
    try:
        data = await _request_with_retry("GET", "/peers", max_wait=3.0)
        return data.get("peers", [])
    except Exception:  # noqa: BLE001 — no peers / gateway hiccup → no peer tools
        return []


async def _peer_tools(peer: dict[str, Any]) -> list[Tool]:
    """Fetch one peer's collapsed catalog from its edge, namespaced ``@<peer>``.

    Reads the peer's httpsfront edge directly over CA-verified TLS with a bearer
    fetched over SSH. Best-effort: returns ``[]`` for an unreachable peer.
    """
    from awm import gatewayclient

    name = peer["name"]
    edge = peer["edge_url"].rstrip("/")
    alias = peer.get("ssh_alias") or name
    try:
        bearer = await asyncio.to_thread(gatewayclient.fetch_peer_cred, alias)
        ca = gatewayclient._peer_ca()
        async with httpx.AsyncClient(timeout=10.0, verify=ca) as cli:
            r = await cli.get(f"{edge}/tools", params={"view": "domains"},
                              headers={"Authorization": f"Bearer {bearer}"})
            r.raise_for_status()
        out: list[Tool] = []
        for t in (r.json() or {}).get("tools", []):
            t["name"] = f'{t["name"]}@{name}'
            out.append(Tool.model_validate(t))
        return out
    except Exception as exc:  # noqa: BLE001 — peer down / no cred / bad TLS
        # Stderr only (stdout is the MCP stdio channel); never raise.
        print(f"awm-mcp: peer {name!r} tools unavailable: {exc}", file=sys.stderr)
        return []


async def _peer_invoke(peer_name: str, base_name: str, arguments: dict,
                       as_: str | None) -> dict[str, Any]:
    """Invoke ``base_name`` on ``peer_name``'s edge directly. Re-fetches the
    credential once on a 401 (it may have rotated)."""
    from awm import gatewayclient

    entry = await asyncio.to_thread(gatewayclient.resolve_peer, peer_name)
    edge = entry["edge_url"].rstrip("/")
    alias = entry.get("ssh_alias") or peer_name
    ca = gatewayclient._peer_ca()
    resp = None
    for attempt in (0, 1):
        bearer = await asyncio.to_thread(
            lambda: gatewayclient.fetch_peer_cred(alias, force=(attempt == 1)))
        headers = {"Authorization": f"Bearer {bearer}"}
        if as_:
            headers["X-Awm-As"] = as_
        async with httpx.AsyncClient(timeout=60.0, verify=ca) as cli:
            resp = await cli.post(f"{edge}/invoke",
                                  json={"name": base_name, "args": arguments},
                                  headers=headers)
        if resp.status_code == 401 and attempt == 0:
            continue
        break
    resp.raise_for_status()
    return resp.json()


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
    # Port-check: if something is already listening on :7819, don't spawn
    # a duplicate. (The status loop above will recover when the existing
    # process becomes responsive.)
    import socket as _socket
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        try:
            s.connect(("127.0.0.1", 7819))
            return
        except OSError:
            pass
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
        await server.run(read, write, server.create_initialization_options())


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
