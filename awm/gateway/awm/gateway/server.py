"""FastAPI app + uvicorn with lifespan management.

This is the gateway's HTTP surface. It exposes only what the gateway owns
itself — the daemon lifecycle (`/status`, `/restart`), the generic tool
dispatch the MCP proxy rides (`/tools`, `/invoke`, both backed by the live
`catalog`), and the hub control plane + routing middleware. Feature surfaces
(scopes, artifacts, …) are NOT baked in here — they arrive as services
register into the catalog/hub. See `catalog.py` for the registration contract.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request

from awm.config import (
    HOST,
    PORT,
    PID_FILE,
    LOG_FILE,
    WORKSPACE_ROOT,
    IDLE_SHUTDOWN_SECONDS,
)
from awm.gateway import catalog, mcp_caller, peer_catalog
from awm.gateway.gateway_ops import GATEWAY_OPERATIONS
from awm.gateway.operations import register_fastapi_routes

__version__ = "0.1.0"

# ---------------------------------------------------------------------------
# Idle shutdown state
# ---------------------------------------------------------------------------

_last_request_time: float = 0.0
_shutdown_event: asyncio.Event | None = None

# Grace window the drain waits for services to drop their leases (exit) after
# the in-band stand-down frame, before force-killing a straggler. Module-level
# so tests can shrink it.
_GRACE_DEADLINE_S: float = 8.0


def _pid_alive(pid: int | None) -> bool:
    """True iff ``pid`` names a live process. ``os.kill(pid, 0)`` raises
    ``ProcessLookupError`` for a dead PID and ``PermissionError`` for one we
    can't signal (which still means it's alive)."""
    if not pid or pid <= 0:
        return False
    import os
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def _idle_shutdown_loop():
    """Shut down the server after a period of inactivity."""
    global _shutdown_event
    _shutdown_event = asyncio.Event()
    while not _shutdown_event.is_set():
        await asyncio.sleep(30)
        if IDLE_SHUTDOWN_SECONDS <= 0:
            continue
        elapsed = time.time() - _last_request_time
        if elapsed > IDLE_SHUTDOWN_SECONDS:
            print(f"[idle] No requests for {int(elapsed)}s — shutting down")
            _shutdown_event.set()
            # Stand our services down in-band first: their control WSs are
            # still open at this point (it's the os._exit below that skips
            # lifespan, not anything that closes connections), so the
            # shutdown frame reaches them and they exit themselves; only a
            # straggler is force-killed.
            await _drain_services()
            # Force exit since uvicorn doesn't have a clean programmatic shutdown
            import os
            os._exit(0)


# ---------------------------------------------------------------------------
# Graceful service drain (the in-band stand-down path)
# ---------------------------------------------------------------------------

async def _drain_services() -> None:
    """Stand the gateway's services down over their own control connections,
    then force-kill any that ignore the frame, then clear the journal.

    This is the single graceful-stop coroutine, reused by three callers: the
    signal override (the real path on Linux — runs *before* uvicorn closes the
    control WSs, so the in-band frame is actually delivered), the idle self-stop
    (T3), and the lifespan-shutdown backstop (for TestClient / non-main-thread
    where the override can't install).

    Idempotent: a second call finds no live leases and an empty journal, so it
    is a no-op. Wrapped so teardown can never wedge process exit.
    """
    try:
        from awm.gateway.hub import rpc, supervisor
        from awm.gateway.hub.lease import get_lease_manager
        from awm.gateway.hub.registry import get_registry

        supervisor.set_shutting_down(True)
        registry = get_registry()
        lm = get_lease_manager()

        # 1. Send each live, non-overlay service an in-band stand-down frame.
        #    Overlays belong to a live `awm dev shadow` process — not ours to
        #    kill. The control-WS writer task delivers the frame as long as the
        #    socket is still open (it is, when called from the signal override).
        live = [
            rec for rec in await registry.list()
            if rec.kind == "service" and not rec.is_overlay
            and lm.is_held(rec.service_id)
        ]
        for rec in live:
            ch = rpc.get_control(rec.service_id)
            if ch is not None:
                ch.enqueue({"kind": "shutdown"})
        if live:
            print(f"[awm] shutdown: signalled {len(live)} service(s); waiting")

        # 2. Wait for them to drop their leases (exit), up to a grace window.
        _grace_deadline = time.monotonic() + _GRACE_DEADLINE_S
        while time.monotonic() < _grace_deadline:
            if not any(lm.is_held(r.service_id) for r in live):
                break
            await asyncio.sleep(0.2)

        # 3. Force-kill any straggler that did NOT stand down. A service that
        #    obeyed the in-band frame has exited — its lease dropped and its
        #    process is gone — so it is left untouched (the frame did the work).
        #    A straggler is one still holding its lease (ignored the frame) OR
        #    whose process is still alive while its lease is already gone. The
        #    latter is the backstop path: when the signal override could not
        #    install, uvicorn closes the control WSs (releasing leases) before
        #    this runs, so lease-held alone would miss live orphans — the
        #    pid-alive check catches them. Force-kill is the backstop here, not
        #    the primary mechanism.
        journal = supervisor.load_service_journal()
        for name, entry in journal.items():
            if not isinstance(entry, dict):
                continue
            sid = entry.get("service_id")
            pid = entry.get("last_pid")
            if not pid:
                continue
            held = bool(sid) and lm.is_held(sid)
            if held or _pid_alive(pid):
                print(f"[awm] shutdown: {name} did not stand down; "
                      f"force-killing pid={pid}")
                supervisor.kill_pid_group(pid)

        # 4. Clear the journal so the next boot bootstraps a clean set instead
        #    of waiting out reconcile's window against dead PIDs.
        supervisor.write_service_journal({})
    except Exception as exc:  # noqa: BLE001 — teardown must never wedge exit
        print(f"[awm] graceful service shutdown skipped: {exc}")


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _last_request_time
    _last_request_time = time.time()

    # Record core start time for awm_status uptime reporting
    catalog.mark_core_start()

    # Write PID file
    import os
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    # Own SIGTERM / SIGINT at the event-loop level so we can DRAIN our services
    # in-band before uvicorn tears their control WSs down.
    #
    # The problem this solves: uvicorn installs its own SIGTERM/SIGINT handlers
    # BEFORE lifespan startup (inside `with self.capture_signals():`, which wraps
    # startup), and on signal it sets should_exit and closes every active
    # connection — including the service control WSs — and only *then* runs
    # lifespan shutdown. So enqueuing the in-band shutdown frame from lifespan
    # shutdown reaches no one (the writer tasks are already gone).
    #
    # Fix: take ownership of the signal so uvicorn's handle_exit does NOT run on
    # the first signal. We capture uvicorn's Server off its installed handler,
    # then register ours via loop.add_signal_handler — which replaces uvicorn's
    # registration and runs our callback in loop context (safe to spawn a task;
    # works on uvloop too). On the first signal we set the shutting-down flag (so
    # the crash watchdog can't race the teardown) and launch _drain_then_stop —
    # which drains services WHILE the WSs are still open, then flips
    # server.should_exit so uvicorn finishes the shutdown. A second signal
    # force-exits immediately (operator escape hatch).
    import signal as _signal
    from awm.gateway.hub import supervisor as _sup

    loop = asyncio.get_running_loop()

    # Capture uvicorn's Server off the handler it installed. This uvicorn version
    # registers `signal.signal(sig, server.handle_exit)` (it explicitly uses
    # signal.signal even when add_signal_handler is available), so during lifespan
    # startup signal.getsignal(sig) returns the bound handle_exit and its
    # __self__ is the Server. Guarded — "couldn't capture" triggers the fallback,
    # never a crash. The should_exit attr check ensures we only treat a genuine
    # uvicorn Server as captured (not SIG_DFL / an unrelated callable).
    _server = None
    for _sig in (_signal.SIGTERM, _signal.SIGINT):
        try:
            _prev = _signal.getsignal(_sig)
            _candidate = getattr(_prev, "__self__", None)
        except Exception:  # noqa: BLE001
            _candidate = None
        if _candidate is not None and hasattr(_candidate, "should_exit"):
            _server = _candidate
            break

    if _server is not None:
        _drain_scheduled = {"v": False}

        async def _drain_then_stop() -> None:
            # uvicorn's should_exit stays False until the drain finishes, so it
            # has NOT started closing connections — the control-WS writer tasks
            # are alive and the enqueued frames are delivered. Only then do we
            # flip should_exit to let uvicorn finish (close any non-service
            # connections, run the now-no-op lifespan backstop, exit).
            try:
                await _drain_services()
            finally:
                _server.should_exit = True

        def _on_signal() -> None:
            _sup.set_shutting_down(True)
            if _drain_scheduled["v"]:
                # Second signal — escape hatch: stop now, don't wait the drain.
                _server.should_exit = True
                _server.force_exit = True
                return
            _drain_scheduled["v"] = True
            asyncio.create_task(_drain_then_stop())

        _owned = False
        for _sig in (_signal.SIGTERM, _signal.SIGINT):
            try:
                loop.add_signal_handler(_sig, _on_signal)
                _owned = True
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        if not _owned:
            _server = None  # couldn't install — fall through to the fallback

    if _server is None:
        # Fallback (non-unix loop, TestClient, or asyncio internals changed):
        # can't defer uvicorn, so degrade to the pre-existing flag-only wrapper.
        # The flag still suppresses the crash watchdog; the actual drain then
        # runs from the lifespan-shutdown backstop (force-kill path, as before).
        def _wrap_signal(sig: int) -> None:
            prev = _signal.getsignal(sig)

            def _handler(signum, frame):  # noqa: ANN001
                _sup.set_shutting_down(True)
                if callable(prev):
                    prev(signum, frame)

            try:
                _signal.signal(sig, _handler)
            except (ValueError, OSError):
                # Not on the main thread (e.g. under TestClient) — the flag is
                # still set explicitly in the shutdown half below.
                pass

        for _sig in (_signal.SIGTERM, _signal.SIGINT):
            _wrap_signal(_sig)

    # Start background tasks
    idle_task = asyncio.create_task(_idle_shutdown_loop())

    # Bring feature services up. One background task runs two phases in order:
    #   1. reconcile journaled services — give each a 10s window to reopen its
    #      control WS, then respawn silent ones from start_cmd;
    #   2. bootstrap discovered-but-unjournaled services — first-boot self-heal
    #      for a fresh clone / wiped journal / newly-added service folder.
    # Sequential (bootstrap awaits reconcile) so bootstrap reads the journal
    # only after reconcile's window + respawns have settled — no double-spawn.
    async def _bring_up_services() -> None:
        from awm.gateway.hub.supervisor import (
            bootstrap_discovered_pages,
            bootstrap_discovered_services,
            reconcile_journaled_services,
            self_heal_loop,
        )
        await reconcile_journaled_services()
        await bootstrap_discovered_services()
        # 2b. Register discovered page bundles (/ui/<name>). Pages hold no
        #     control WS and are never journaled, so this filesystem re-derive
        #     is what brings them back after a restart.
        await bootstrap_discovered_pages()
        # 3. The two standing background sweeps, started under
        #    ``spawn_supervised`` — a bare create_task that dies on its first
        #    line leaves the gateway looking healthy with a whole capability
        #    silently absent, which is exactly the class of fault these sweeps
        #    exist to catch.
        #      self_heal_loop — re-bootstrap a service later found wedged (dead
        #        PID, no ready control), covering crashes that bypass the
        #        control-WS disconnect watchdog.
        #      reap_loop — kill orphaned hub_adapter processes targeting this
        #        origin. Recovery must not depend on an operator noticing and
        #        typing `awm services reap`.
        #      peer_catalog.refresh_loop — which peer provides which MCP domain.
        #        A sweep costs an ssh plus a TLS round trip to a host that may be
        #        asleep, so it is kept off the request path entirely: `/tools` and
        #        providersOf read whatever this last produced.
        from awm.gatewayclient import spawn_supervised

        from awm.gateway.gateway_ops import reap_loop
        spawn_supervised("gateway/self-heal", self_heal_loop)
        spawn_supervised("gateway/reap", reap_loop)
        spawn_supervised("gateway/peer-catalog", peer_catalog.refresh_loop)

    try:
        asyncio.create_task(_bring_up_services())
    except Exception as exc:  # noqa: BLE001
        print(f"[awm] service bring-up skipped: {exc}")

    # Fan canonical .mcp.json out to backend-specific configs.
    try:
        from awm.gateway.exports.mcp import sync_mcp_configs
        for entry in sync_mcp_configs():
            name = entry.get("name", "?")
            if entry.get("ok"):
                print(f"[awm] mcp-sync {name} → {entry.get('path')}")
            else:
                print(f"[awm] mcp-sync {name} FAILED: {entry.get('error')}")
    except Exception as exc:  # noqa: BLE001
        print(f"[awm] mcp-sync skipped: {exc}")

    yield

    # ----- Graceful shutdown backstop ---------------------------------------
    # On Linux the signal override above has already drained services in-band
    # (before uvicorn closed their control WSs), so this call is the idempotent
    # no-op tail. It remains the ONLY drain in the cases the override can't
    # install — under TestClient / a non-main-thread loop — where it runs the
    # force-kill path as before. Under a hard SIGKILL or the idle os._exit this
    # half is skipped entirely, which is why the service-side reconnect deadline
    # (T2 give-up) is the required backstop, not optional.
    await _drain_services()

    # Cleanup
    idle_task.cancel()
    if PID_FILE.exists():
        PID_FILE.unlink()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="AWM", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def track_activity(request, call_next):
    global _last_request_time
    _last_request_time = time.time()
    return await call_next(request)


# ---------------------------------------------------------------------------
# Generated control-plane routes
# ---------------------------------------------------------------------------
# The gateway's own control ops (status / restart / mcp-sync / hub list +
# deregister / services lifecycle) are declared once in GATEWAY_OPERATIONS and
# their HTTP routes generated here — GET /status, POST /restart, POST /mcp-sync,
# GET /hub/services, DELETE /hub/services/{name}, POST /hub/services/{name}/*.
# No hand-rolled duplicates: the same Operation also drives the MCP tool
# (catalog) and the CLI command (cli.py). Add a new control op as an Operation,
# not a hand-written route here.
register_fastapi_routes(app, GATEWAY_OPERATIONS)


# ---------------------------------------------------------------------------
# Generic tool dispatch (used by the thin MCP stdio proxy)
# ---------------------------------------------------------------------------

@app.get("/tools")
def list_tools_endpoint(view: str | None = None, peers: int = 0):
    """Return the current MCP tool definitions from the live catalog.

    The thin stdio proxy fetches this on every `list_tools` call instead of
    caching at its own startup. That keeps the proxy stateless: tools that
    appear/vanish as services register show up immediately, with no Claude
    Code restart. Sync over a GIL-safe registry snapshot — see catalog.py.

    ``view=domains`` returns the collapsed per-domain projection (one generic
    ``{verb, args, peer}`` tool per domain — what the MCP proxy advertises so a
    non-deferring client carries a few dozen tools instead of hundreds). Any other
    value (default) returns the expanded per-verb surface, which the CLI generator
    and the flat ``/invoke`` dispatch still depend on.

    ``peers=1`` widens the collapsed view to the fleet: peer-only domains appear
    and each tool says where it runs by default. It stays **opt-in** for two
    reasons — a peer reading *our* catalog must get the local-only view (or the
    fleet would advertise transitive peers this node cannot dial), and no existing
    consumer of the plain view changes shape. Still sync: the peer data comes from
    a background snapshot, so this route never waits on a peer even cold.
    """
    if view == "domains":
        tools = catalog.list_domain_tools(peers=bool(peers))
    else:
        tools = catalog.list_tools()
    return {"tools": [t.model_dump(by_alias=True) for t in tools]}


# The expanded surface names a verb `<domain>_<verb>`, and service names cannot
# contain an underscore, so this prefix is exactly reflection's verbs — including
# ones added later, which a hand-maintained list would silently miss.
_REFLECTION_FLAT_PREFIX = "reflection_"


def _stamp_reflection_caller(name: str, args: dict, pid_header: str | None,
                             descendant_header: str | None = None) -> None:
    """Stamp the calling session's own pid onto a reflection call.

    `awm-mcp` runs as a stdio child of the session that calls it, so it forwards
    its parent pid as `X-Awm-Session-Pid`; that identifies the caller regardless
    of whether it is hosted in a tmux pane or as a background job. Reflection is
    the only domain whose contract with the model requires zero awareness of any
    of this — calls arrive carrying nothing about who is making them, and this is
    the one place identity is attached before dispatch.

    The value is always *overwritten*, and stripped entirely when no header is
    present, so `_caller_pid` cannot be supplied from the model side. That is the
    point: reflection injects into the caller's own prompt, so being able to name
    a different target would turn it into a way to type into other agents.
    Scoped to reflection only — no other service's args are touched. Mutates
    ``args`` in place (mirrors how the flat/domain shapes already nest it).

    ``X-Awm-Caller-Pid`` is the opt-in second door, for a caller that is *some
    descendant* of the session rather than the proxy itself — a Claude Code hook,
    whose own pid names no session and is refused outright otherwise. That pid is
    walked to the nearest ancestor holding a session record, exactly as the proxy
    walks its own. It stays a separate header rather than a widening of the first
    on purpose: running the walk on ``X-Awm-Session-Pid`` would turn its
    fail-closed refusal (a pid with no record) into a climb to whatever *ancestor*
    session exists, which for a nested agent is the parent's prompt. Opt-in keeps
    the walk to callers that asked for it, and the resolved pid is still only a
    narrowing step — reflection re-reads the record and checks it before typing.
    ``X-Awm-Session-Pid`` wins when both are present.
    """
    if name == "reflection":
        inner = args.get("args")
        if not isinstance(inner, dict):
            inner = {}
            args["args"] = inner
    elif name.startswith(_REFLECTION_FLAT_PREFIX):
        inner = args
    else:
        return
    if pid_header and pid_header.isdigit():
        inner["_caller_pid"] = int(pid_header)
    elif descendant_header and descendant_header.isdigit():
        inner["_caller_pid"] = mcp_caller.resolve_caller_pid(int(descendant_header))
    else:
        inner.pop("_caller_pid", None)


@app.post("/invoke")
async def invoke_tool(payload: dict, request: Request):
    """Dispatch an MCP-style tool call by name through the catalog. Async so
    service ops can be awaited over their control WS on the server loop (no
    second event loop — see catalog.py concurrency note). The MCP proxy
    forwards here over HTTP so the core can restart without tearing down the
    stdio pipe Claude Code has open."""
    name = payload.get("name")
    args = payload.get("args", {}) or {}
    if not name:
        raise HTTPException(400, "missing 'name' in payload")
    as_ = request.headers.get("X-Awm-As")
    _stamp_reflection_caller(name, args, request.headers.get("X-Awm-Session-Pid"),
                             request.headers.get("X-Awm-Caller-Pid"))
    try:
        result = await catalog.dispatch(name, args, as_=as_)
    except peer_catalog.PeerRedirect as e:
        # The call belongs to a peer. The gateway resolves, never relays — so a
        # caller that told us it can dial a peer edge (only `awm-mcp` does, via
        # this header) gets the address back and makes the call itself, keeping
        # the invariant that no peer bytes traverse a gateway. Anyone else gets
        # 421 Misdirected Request, which is literally this condition, rather than
        # a silent local run — that would be the half-route failure the whole
        # default-provider model exists to prevent.
        if request.headers.get("X-Awm-Peer-Redirect"):
            return {"peer_redirect": e.payload()}
        raise HTTPException(421, str(e))
    except ValueError as e:
        # Unknown tool name -> 404
        raise HTTPException(404, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    except Exception as e:  # noqa: BLE001 — surface anything else with class+message
        # Inbox #232: bare {"error": "Internal Server Error"} hid the real
        # failure from MCP callers. Log the traceback server-side and put a
        # structured {error_class, error} in the response detail so the
        # caller can branch on the exception class.
        import traceback as _tb
        print(
            f"[awm] /invoke {name} failed with {type(e).__name__}: {e}\n"
            f"{_tb.format_exc()}",
            flush=True,
        )
        raise HTTPException(
            status_code=500,
            detail={"error_class": type(e).__name__, "error": str(e), "tool": name},
        )
    return {"result": result}


# ---------------------------------------------------------------------------
# Hub control plane (/hub/*)
# ---------------------------------------------------------------------------

from awm.gateway.api.hub import router as hub_router  # noqa: E402

app.include_router(hub_router)


# ---------------------------------------------------------------------------
# Hub forwarding middleware (outermost — empty-registry pass-through is
# byte-identical to a hub-less awm). Routes /ui/<page>, /svc/<name>/...,
# and any URL/static prefix that's been registered. Raw ASGI so HTTP and
# WebSocket scopes are both handled.
# ---------------------------------------------------------------------------

from awm.gateway.hub.proxy import proxy_http, proxy_ws  # noqa: E402
from awm.gateway.hub.registry import get_registry as _get_hub_registry  # noqa: E402
from awm.gateway.hub.static import (  # noqa: E402
    close_ws_unsupported as _ws_close_unsupported,
    serve_static as _serve_static,
)


class HubRoutingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        registry = _get_hub_registry()
        if registry.is_empty():
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path == "/hub" or path.startswith("/hub/"):
            return await self.app(scope, receive, send)
        rec = registry.longest_match(path)
        if rec is None:
            return await self.app(scope, receive, send)
        if rec.kind in ("static", "page"):
            if scope["type"] == "websocket":
                await _ws_close_unsupported(scope, receive, send)
                return
            request = Request(scope, receive=receive)
            response = await _serve_static(request, rec)
            await response(scope, receive, send)
            return
        if rec.kind == "service":
            await self._dispatch_service(scope, receive, send, rec, path)
            return
        strip = rec.prefix if rec.strip_prefix else ""
        if scope["type"] == "http":
            request = Request(scope, receive=receive)
            response = await proxy_http(request, rec.url, prefix=strip)
            await response(scope, receive, send)
        else:
            from fastapi import WebSocket as _WS
            ws = _WS(scope, receive=receive, send=send)
            await proxy_ws(ws, rec.url, prefix=strip)

    async def _dispatch_service(self, scope, receive, send, rec, path):
        """RPC-WS service routing (kind="service"):

        - POST <prefix>/fn/<name>            -> control-WS call/notify
        - POST <prefix>/session/<kind>       -> open session, return ws_path
        - WS   <prefix>/session/<id>         -> session WS (direct = byte
                                                relay, otherwise enveloped)
        - WS   <prefix>/emit/<topic>         -> emit subscriber WS
        Everything else under the prefix is 404.
        """
        from awm.gateway.hub.proxy import (
            open_session_via_http,
            proxy_service_emit_ws,
            proxy_service_http,
            proxy_session_ws,
        )
        from fastapi import WebSocket as _WS
        from starlette.datastructures import Headers
        from starlette.responses import PlainTextResponse

        rel = path[len(rec.prefix):]
        # Forward the advisory caller identity (the attaching placement's unit
        # slug) so a service's attach-gated admin relay can resolve the caller.
        # Headers(scope=...) reads the ASGI header list for both http and
        # websocket scopes — the MCP /invoke path reads the same header. Purely
        # advisory: no bearer, no change to the loopback no-auth model.
        as_ = Headers(scope=scope).get("X-Awm-As")

        if scope["type"] == "http":
            request = Request(scope, receive=receive)
            if rel.startswith("/fn/"):
                response = await proxy_service_http(
                    request, rec.service_id, as_=as_,
                )
            elif rel.startswith("/session/") and request.method == "POST":
                response = await open_session_via_http(
                    request, rec.service_id, as_=as_,
                )
            else:
                response = PlainTextResponse("not found", status_code=404)
            await response(scope, receive, send)
            return

        ws = _WS(scope, receive=receive, send=send)
        if rel.startswith("/session/"):
            sid = rel[len("/session/"):]
            await proxy_session_ws(ws, rec.service_id, sid)
            return
        if rel.startswith("/emit/"):
            topic = rel[len("/emit/"):]
            await proxy_service_emit_ws(ws, rec.service_id, topic, as_=as_)
            return
        await _ws_close_unsupported(scope, receive, send)


app.add_middleware(HubRoutingMiddleware)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_server(foreground: bool = True):
    """Start the uvicorn server."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Per-workspace env file: merge $AWM_WORKSPACE/.awm/env into os.environ
    # before the probe and before any subprocess we spawn (notably git
    # clone over SSH from project_create — inbox #236).
    from awm.config import load_env_file
    load_env_file()
    # Pre-bind probe: if something is already on (HOST, PORT), figure out
    # whether it's a healthy awm against the same workspace (→ exit 0) or
    # a foreign holder (→ exit 1 with a diagnostic). Eliminates the silent
    # EADDRINUSE restart-loop pattern (inbox #232).
    from awm.gateway._process_utils import exit_if_healthy_peer
    exit_if_healthy_peer(HOST, PORT, str(WORKSPACE_ROOT))
    # Give the root logger a handler so `awm.*` records reach the log file.
    # uvicorn only configures its own loggers (and marks them non-propagating),
    # so without this every gateway INFO — service spawn, respawn, control-WS
    # open, subscriber teardown — falls through to logging.lastResort and is
    # dropped below WARNING. All 16 sites are lifecycle events, none per-request.
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_level="info",
    )
