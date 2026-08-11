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
# Caller identity
# ---------------------------------------------------------------------------

# Claude Code writes one record per live session at ``~/.claude/sessions/<repl-pid>.json``.
# Existence alone is enough here: this only decides which ancestor to *name*, and
# reflection re-reads the record and checks its ``procStart`` against the live
# process before injecting anywhere (``session_target.read_session_record``). So a
# stale record squatting a recycled pid fails closed there rather than being
# mis-targeted here, and this stays a narrowing step, not an authority.
_SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
_MAX_ANCESTRY_HOPS = 8


def _ppid_of(pid: int) -> int | None:
    """``/proc/<pid>/stat`` field 4 (ppid), or ``None`` if the pid is gone.

    Field 2 (comm) is parenthesised and may itself contain spaces and parens, so
    everything up to the *last* ``)`` is skipped; after it, field N sits at index
    N-3.
    """
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fp:
            data = fp.read()
    except OSError:
        return None
    try:
        return int(data[data.rfind(")") + 2:].split()[1])
    except (IndexError, ValueError):
        return None


def _resolve_caller_pid(start_pid: int, *, sessions_dir: str = _SESSIONS_DIR,
                        ppid_of=_ppid_of,
                        max_hops: int = _MAX_ANCESTRY_HOPS) -> int:
    """The nearest ancestor that is a Claude Code session — usually our parent.

    The proxy is normally a direct stdio child of the REPL, so hop zero hits and
    this costs one ``exists``. That stops holding the moment something wraps the
    launch: an ``.mcp.json`` spelling the command ``mamba run -n awm awm-mcp``
    instead of the absolute interpreter path leaves the wrapper sitting between us
    and the REPL, and naming the wrapper made reflection refuse with "the caller
    does not look like a Claude Code session" — a session losing self-compaction
    because of how its config spells a command, with nothing in the message
    pointing at the cause.

    Walking up fixes that without widening what a model can reach. The chain comes
    from ``/proc`` and never from an argument, and the walk stops at the *first*
    session it meets, so an agent nested inside another resolves to its own REPL
    and never to its parent's. A chain containing no session returns ``start_pid``
    unchanged, so a genuine non-Claude caller refuses exactly as it did before.
    """
    pid: int | None = start_pid
    for _ in range(max_hops):
        if pid is None or pid <= 1:
            break
        if os.path.exists(os.path.join(sessions_dir, f"{pid}.json")):
            return pid
        pid = ppid_of(pid)
    return start_pid


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
    non-deferring client (a spawned agentcore bot, opencode) carries a tiny tool
    surface and learns each domain's verbs + params on demand via the reserved
    ``describe`` verb. The CLI/HTTP surfaces stay expanded (they read the default
    ``/tools``).

    ``peers=1`` makes that surface **fleet-wide** without duplicating it: one tool
    per domain name whatever the peer count, each knowing which node it defaults
    to, plus the reserved ``providersOf``. This proxy used to fan out to every
    peer's edge here and merge their whole catalogs under ``<domain>@<peer>``
    names — two peers tripled the tool list for no new capability, and every
    single ``list_tools`` paid an ssh per peer. The gateway now keeps that map in a
    background snapshot, so this is one loopback GET.
    """
    data = await _request_with_retry(
        "GET", "/tools", params={"view": "domains", "peers": "1"})
    return [Tool.model_validate(t) for t in data["tools"]]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        # A placed agent's proxy carries its placement identity in AWM_AS (set
        # in its per-placement spawn-mcp config). Stamp it as X-Awm-As so the
        # core can resolve the call to that placement (the agents B-op tools
        # need no model-supplied token). Read at call time, not import time, so a
        # reused proxy always reflects its own env. Absent for normal sessions.
        as_ = os.environ.get("AWM_AS")
        # This proxy is spawned by the calling session as a stdio child, so the
        # caller is the nearest Claude Code session above us — our parent when
        # nothing wraps the launch, and the pid `_resolve_caller_pid` walks up to
        # when something does. That is the identity the reflection service
        # targets, and it is the same fact whether the session sits in a tmux
        # pane or runs as a background job, which is what lets an agent reflect
        # on itself without knowing how it happens to be hosted. Read at call
        # time (see AWM_AS above); it is observed here rather than accepted as an
        # argument, so a model cannot aim reflection at anyone but itself.
        session_pid = str(_resolve_caller_pid(os.getppid()))
        # Compatibility shim: a client holding a stale tool list may still name
        # ``<domain>@<peer>``. Those names are no longer advertised (the surface
        # carries one tool per domain with a ``peer`` argument instead), but
        # honouring them costs four lines and avoids a hard break.
        if "@" in name:
            base, _, peer_name = name.rpartition("@")
            data = await _peer_invoke(peer_name, base, arguments, as_)
            return [TextContent(type="text", text=data["result"])]
        headers = {}
        if as_:
            headers["X-Awm-As"] = as_
        if session_pid:
            headers["X-Awm-Session-Pid"] = session_pid
        # Declare that this caller can follow a peer redirect. The gateway
        # resolves where a call belongs (default provider, or an explicit
        # ``peer``) and hands back that peer's address rather than relaying —
        # only a caller that talks to peer edges may opt in, so the CLI and the
        # web UI keep seeing plain results and plain errors.
        headers["X-Awm-Peer-Redirect"] = "1"
        data = await _request_with_retry(
            "POST", "/invoke", json_body={"name": name, "args": arguments},
            headers=headers,
        )
        redirect = data.get("peer_redirect")
        if redirect:
            # Followed exactly once: the peer's own gateway resolves against its
            # own book, and chaining would let two nodes bounce a call forever.
            # ``peer`` is stripped for the same reason — the far side must not
            # re-route what we already routed.
            forwarded = {k: v for k, v in arguments.items() if k != "peer"}
            data = await _peer_invoke(
                redirect["peer"], name, forwarded, as_, entry=redirect)
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

async def _peer_invoke(peer_name: str, base_name: str, arguments: dict,
                       as_: str | None,
                       entry: dict[str, Any] | None = None) -> dict[str, Any]:
    """Invoke ``base_name`` on ``peer_name``'s edge directly. Re-fetches the
    credential once on a 401 (it may have rotated).

    ``entry`` is the peer's already-resolved ``{edge_url, ssh_alias}`` — a
    redirect carries it, so following one costs no second lookup. Absent (the
    legacy ``@peer`` path) it is resolved from the local gateway's book.
    """
    from awm import gatewayclient

    if entry is None or not entry.get("edge_url"):
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
