"""Zero-dependency stdio MCP proxy for the AWM core.

Behaviourally identical to :mod:`awm.gateway.mcp_server_sdk` — same tool
surface, same headers, same error envelopes — but it speaks JSON-RPC over stdio
by hand and talks to the core with :mod:`urllib`, so it imports **no** ``mcp``
SDK, no ``pydantic``, no ``httpx`` on the startup path. Selected by
:mod:`awm.gateway.mcp_server`, which is what the ``awm-mcp`` console script
runs.

Why this exists: an MCP client cannot use *any* tool until the server answers
``initialize``, so every import on that path is dead time added to each session
launch. The SDK-based proxy spends ~0.7s importing a framework in order to
broker a request the core answers in ~19ms. Measured on altair: 0.96s to first
``tools/list`` via the SDK, ~0.15s here.

The design invariant from ``mcp_server`` carries over unchanged and is the
reason this is a *proxy* and not a cache: it holds no tool definitions. Every
``tools/list`` re-fetches from the core, so restarting only the core still
picks up a changed tool surface.

The heavy imports are not deleted, only deferred to the paths that need them:
a cross-peer call still uses ``awm.gatewayclient`` (and thus httpx), imported
on first use. Cross-peer calls are rare and already pay an ssh, so the import
disappears into that cost instead of into every launch.

**One deliberate behaviour change.** The SDK validated ``arguments`` against
the tool's ``inputSchema`` before dispatching — that jsonschema dependency is a
large slice of the import cost being removed here, and validating locally would
mean holding a schema snapshot, which is exactly the stale-surface failure the
stateless design exists to avoid. Arguments are therefore forwarded and the
core validates them: each domain's ``verb`` enum includes the reserved
``describe`` verb alongside its registered verbs, and ``ServiceAdapter._dispatch``
checks a verb's manifest-declared required arguments before invoking its
handler, so a missing argument fails with a clear named-parameter message
instead of a jsonschema one.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from awm import config
from awm.gateway import mcp_caller

SERVER_NAME = "awm"
SERVER_VERSION = "0.1.0"

# Newest first. An ``initialize`` naming one of these is answered with that
# same version; anything else (a client newer than us) is answered with our
# newest and left to decide whether it can live with it — which is what the
# SDK's negotiation does.
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

_stdout_lock = threading.Lock()


# ---------------------------------------------------------------------------
# JSON-RPC transport
# ---------------------------------------------------------------------------

def _send(msg: dict) -> None:
    """Write one newline-delimited JSON message to stdout.

    Locked because requests are served on separate threads: two interleaved
    partial writes would corrupt the stream irrecoverably.
    """
    data = json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n"
    with _stdout_lock:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def _reply(req_id, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


# ---------------------------------------------------------------------------
# HTTP dispatch with reconnect
# ---------------------------------------------------------------------------

class _HTTPStatusError(Exception):
    """Non-2xx from the core. Carries the decoded body for error surfacing."""

    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


def _request_with_retry(
    method: str,
    path: str,
    json_body: dict | None = None,
    max_wait: float = 10.0,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> dict:
    """Request the local awm core, reconnecting across restarts.

    On a transport error the first attempt nudges systemd to start the core,
    then retries until ``max_wait`` so the caller's request transparently
    survives a ``systemctl restart``. A non-2xx response is *not* retried —
    the core answered, and that answer is the result.
    """
    url = config.BASE_URL.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = None
    hdrs = dict(headers or {})
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"

    deadline = time.monotonic() + max_wait
    last_err: Exception | None = None
    first_attempt = True
    while time.monotonic() < deadline:
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60.0) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise _HTTPStatusError(e.code, e.read().decode("utf-8", "replace")) from e
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            last_err = e
            if first_attempt:
                _ensure_core_running()
                first_attempt = False
            time.sleep(0.3)
    raise RuntimeError(f"awm daemon unreachable after {max_wait}s: {last_err}")


# ---------------------------------------------------------------------------
# Cross-peer federation (client-side, no relay)
# ---------------------------------------------------------------------------

def _peer_invoke(peer_name: str, base_name: str, arguments: dict,
                 as_: str | None, entry: dict | None = None) -> dict:
    """Invoke ``base_name`` on ``peer_name``'s edge directly.

    ``gatewayclient`` (and the httpx + TLS machinery under it) is imported here
    rather than at module scope: this is the one path that needs it, and it
    already costs an ssh for the credential, so the import is free by
    comparison and stays off every launch.
    """
    import httpx  # noqa: PLC0415 — deferred on purpose; see module docstring
    from awm import gatewayclient  # noqa: PLC0415

    if entry is None or not entry.get("edge_url"):
        entry = gatewayclient.resolve_peer(peer_name)
    edge = entry["edge_url"].rstrip("/")
    alias = entry.get("ssh_alias") or peer_name
    ca = gatewayclient._peer_ca()
    resp = None
    for attempt in (0, 1):
        bearer = gatewayclient.fetch_peer_cred(alias, force=(attempt == 1))
        headers = {"Authorization": f"Bearer {bearer}"}
        if as_:
            headers["X-Awm-As"] = as_
        with httpx.Client(timeout=60.0, verify=ca) as cli:
            resp = cli.post(f"{edge}/invoke",
                            json={"name": base_name, "args": arguments},
                            headers=headers)
        if resp.status_code == 401 and attempt == 0:
            continue
        break
    if resp.status_code >= 400:
        raise _HTTPStatusError(resp.status_code, resp.text)
    return resp.json()


def _localize(result: Any, peer_name: str, as_: str | None,
              entry: dict | None = None) -> Any:
    """Pull down any file the peer's reply points at, so ``path`` is openable here.

    Deferred import for the same reason as ``_peer_invoke``'s: this is the only
    path that needs it, and it is already paying for an ssh. Never raises — a
    reply with no files passes straight through.
    """
    from awm.gateway import peer_files  # noqa: PLC0415 — deferred on purpose

    try:
        return peer_files.materialize(result, peer_name, entry=entry, as_=as_)
    except Exception as exc:  # noqa: BLE001 — a broken rewrite must not eat the reply
        # stdout is the MCP framing; diagnostics go to stderr only.
        print(f"awm-mcp: peer file materialization failed for {peer_name}: {exc}",
              file=sys.stderr)
        return result


# ---------------------------------------------------------------------------
# MCP method handlers
# ---------------------------------------------------------------------------

def _handle_initialize(params: dict) -> dict:
    requested = params.get("protocolVersion")
    version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
    return {
        "protocolVersion": version,
        "capabilities": {"experimental": {}, "tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def _handle_tools_list() -> dict:
    """Fetch the tool surface from the core on every call — no local cache.

    A stale list is worse than an honest error: it silently drifts from the
    live surface after a core restart, so an unreachable core propagates as an
    error rather than falling back to a snapshot.

    ``view=domains`` asks for the collapsed one-tool-per-domain surface;
    ``peers=1`` makes it fleet-wide from the gateway's background snapshot
    (one loopback GET, not an ssh per peer).
    """
    data = _request_with_retry(
        "GET", "/tools", params={"view": "domains", "peers": "1"})
    # The core emits the optional Tool fields it has nothing to say about as
    # explicit nulls (``annotations``, ``icons``, ``_meta``, …). The SDK proxy
    # dropped them as a side effect of round-tripping through pydantic with
    # exclude_none; forwarding them verbatim instead would hand clients a
    # visibly different tool surface, so strip them here on purpose.
    return {"tools": [{k: v for k, v in t.items() if v is not None}
                      for t in data["tools"]]}


def _handle_tools_call(params: dict) -> dict:
    name = params.get("name", "")
    arguments = params.get("arguments") or {}
    try:
        # A placed agent's proxy carries its placement identity in AWM_AS (set
        # in its per-placement spawn-mcp config). Read at call time, not import
        # time, so a reused proxy always reflects its own env.
        as_ = os.environ.get("AWM_AS")
        # We are spawned by the calling session as a stdio child, so our parent
        # pid is normally the caller — the identity the reflection service
        # targets, observed here rather than accepted as an argument so a model
        # cannot aim reflection at anyone but itself. Normally, but not always:
        # a wrapper in the configured command sits between us and the REPL, so
        # walk up to the nearest ancestor that is one (see mcp_caller).
        session_pid = str(mcp_caller.resolve_caller_pid(os.getppid()))
        # Compatibility shim for a client holding a stale tool list that still
        # names ``<domain>@<peer>``.
        if "@" in name:
            base, _, peer_name = name.rpartition("@")
            data = _peer_invoke(peer_name, base, arguments, as_)
            return {"content": [{"type": "text",
                                 "text": _localize(data["result"], peer_name, as_)}],
                    "isError": False}
        headers = {}
        if as_:
            headers["X-Awm-As"] = as_
        if session_pid:
            headers["X-Awm-Session-Pid"] = session_pid
        # Declare that this caller can follow a peer redirect — only a caller
        # that talks to peer edges may opt in, so the CLI and web UI keep
        # seeing plain results.
        headers["X-Awm-Peer-Redirect"] = "1"
        data = _request_with_retry(
            "POST", "/invoke", json_body={"name": name, "args": arguments},
            headers=headers,
        )
        redirect = data.get("peer_redirect")
        if redirect:
            # Followed exactly once; ``peer`` is stripped so the far side does
            # not re-route what we already routed.
            forwarded = {k: v for k, v in arguments.items() if k != "peer"}
            data = _peer_invoke(
                redirect["peer"], name, forwarded, as_, entry=redirect)
            # The call ran elsewhere, so any file it produced is on that node.
            # Local calls skip this entirely — their paths are already ours.
            text = _localize(data["result"], redirect["peer"], as_, entry=redirect)
            return {"content": [{"type": "text", "text": text}], "isError": False}
        return {"content": [{"type": "text", "text": data["result"]}],
                "isError": False}
    except _HTTPStatusError as e:
        # Core returned a structured error — surface its body. A structured
        # {error_class, error, ...} detail is passed through directly so the
        # caller sees the real exception class instead of a bare 500.
        try:
            detail = json.loads(e.body).get("detail", str(e))
        except Exception:
            detail = e.body or str(e)
        text = json.dumps(detail) if isinstance(detail, dict) else json.dumps({"error": detail})
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except Exception as e:
        return {"content": [{"type": "text", "text": json.dumps({"error": str(e)})}],
                "isError": False}


def _dispatch(msg: dict) -> None:
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    # A notification (no id) is never answered — replying to one is a protocol
    # violation that some clients treat as a fatal stream error.
    if req_id is None:
        return

    try:
        if method == "initialize":
            _reply(req_id, _handle_initialize(params))
        elif method == "ping":
            _reply(req_id, {})
        elif method == "tools/list":
            _reply(req_id, _handle_tools_list())
        elif method == "tools/call":
            _reply(req_id, _handle_tools_call(params))
        else:
            # Matches the SDK's behaviour for a server that registers no
            # resource/prompt handlers: probes get method-not-found, not a hang.
            _error(req_id, -32601, "Method not found")
    except Exception as e:
        _error(req_id, -32603, str(e))


# ---------------------------------------------------------------------------
# Core lifecycle
# ---------------------------------------------------------------------------

def _ensure_core_running() -> None:
    """Start the awm core via systemd if it is not already up.

    Checks the listener first — cheap and instant when the core is already
    running, which is the common case — before falling back to systemd, then
    to a detached ``awm gateway serve`` in dev setups without systemd.
    """
    import socket as _socket  # noqa: PLC0415
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        try:
            s.connect(("127.0.0.1", 7819))
            return
        except OSError:
            pass
    try:
        r = subprocess.run(
            ["systemctl", "--user", "start", "awm.service"],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        r = None
    if r is not None and r.returncode == 0:
        return
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        try:
            s.connect(("127.0.0.1", 7819))
            return
        except OSError:
            pass
    from awm.gateway._path import resolve_bin  # noqa: PLC0415
    subprocess.Popen(
        [resolve_bin("awm"), "gateway", "serve"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _reap_orphan_clients() -> None:
    """SIGTERM sibling proxies whose parent pid no longer exists.

    Leftovers accumulate when a client is killed without giving its MCP
    children time to clean up. Only parentless orphans are killed (ppid 1 means
    the original parent died), never a live session.
    """
    try:
        mypid = os.getpid()
        out = subprocess.run(
            ["pgrep", "-f", "awm-mcp|mcp_stdio"],
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

def main() -> None:
    # Per-workspace env file: merge $AWM_WORKSPACE/.awm/env into os.environ so
    # a fallback-spawned core inherits it.
    config.load_env_file()
    # Both are off the critical path to answering ``initialize`` — pgrep alone
    # costs ~60ms, which is a third of this proxy's whole startup budget — so
    # they run alongside the read loop instead of in front of it.
    threading.Thread(target=_reap_orphan_clients, daemon=True).start()
    threading.Thread(target=_ensure_core_running, daemon=True).start()

    for raw in sys.stdin.buffer:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            _send({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "Parse error"}})
            continue
        # One thread per request: the client pipelines concurrent tool calls,
        # and serving them in the read loop would serialise every fan-out
        # behind the slowest call.
        threading.Thread(target=_dispatch, args=(msg,), daemon=True).start()


if __name__ == "__main__":
    main()
