"""Real-browser engine for the rlm-browser realm — raw CDP over a swappable
launch seam.

Three layers, bottom-up:

1. :class:`LaunchBackend` — the *only* thing that differs between simple/rendered
   and host/docker. Its whole contract: launch a Chrome and hand back a CDP base
   URL on ``127.0.0.1:<port>``; stop it on release. :class:`HostSubprocessBackend`
   is the T2 implementation (a plain ``google-chrome`` subprocess); the docker
   backend (T3) slots in here unchanged above.

2. :class:`CDPConnection` — a minimal DevTools-protocol client over one
   websocket: ``call(method, params)`` round-trips a request/reply by id, and
   unsolicited protocol events are handed to an ``on_event`` sink. No Playwright
   — the point of this service is to *relay the engine's own protocol*, not wrap
   it in a higher-level API.

3. :class:`BrowserManager` — keyed by ``session_id``, owns the backend handle +
   the live per-tab connections, and implements the realm verbs:
   acquire/release/reset, tab_*, observe/screenshot, and the ``cdp``/``commands``
   relay. Tab management rides the browser-level ``Target.*`` domain (stable
   across Chrome versions, unlike the drifting ``/json/new`` HTTP verb).

Reconnect: the manager cache is in-process, but a session's DB row persists its
``cdp_port`` + ``mode``, so after a service restart the first verb touching a
session lazily re-attaches to the still-running Chrome over CDP — no fresh
``acquire`` needed.

Loopback only: Chrome ≥111 rejects DevTools requests whose Host header isn't an
IP, so every connection is by ``127.0.0.1`` (never a hostname), and the host
port is pinned 1:1 to the debug port so the ``webSocketDebuggerUrl`` Chrome
advertises is dialable as-is.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import urlparse

import httpx
import websockets

from awm.rlm_browser import dao

log = logging.getLogger("awm.rlm_browser.browser")

# Where per-game Chrome profiles live. MUST be on Linux ext4 (never /mnt/c) or
# Chrome's SingletonLock corrupts and tab perf tanks under WSL.
try:
    from awm.config import SERVICES_DIR
except ImportError:  # pragma: no cover
    from awm.config import AWM_DIR
    SERVICES_DIR = AWM_DIR / "services"

_PROFILES_DIR = Path(SERVICES_DIR) / "rlm-browser" / "profiles"

# Chrome binary: explicit override wins, else first found on PATH.
_CHROME_CANDIDATES = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
)

# Backend selector — env so the docker backend is a one-line switch, no code.
# This selects the *launch* backend (host/docker) for the headless/rendered
# modes; the `attached` mode bypasses it for AttachBackend.
_BACKEND = os.environ.get("RLM_BROWSER_BACKEND", "host").strip().lower()

# Canonical modes, fixed for a session's life:
#   headless  — launched, no display (was 'simple'); fast, for bots at scale.
#   rendered  — launched headful + VNC; watchable live.
#   attached  — NOT launched; drives an EXISTING real Chrome over CDP (e.g. the
#               Windows desktop browser). Borrowed: release never closes it.
_DEFAULT_MODE = "headless"
_VALID_MODES = ("headless", "rendered", "attached")

# Default CDP endpoint for `attached` mode — the same already-running desktop
# Chrome the chrome-devtools MCP targets. Overridable via env or acquire opts.
_ATTACH_CDP_URL = os.environ.get(
    "RLM_ATTACH_CDP_URL", "http://172.25.176.1:19222").strip()


def _norm_mode(mode: str | None) -> str:
    """Canonicalize a mode string. Legacy ``simple`` maps to ``headless`` so old
    DB rows and callers still resolve; anything unknown falls back to the
    default."""
    m = (mode or "").strip().lower()
    if m == "simple":
        return "headless"
    return m if m in _VALID_MODES else _DEFAULT_MODE


class CDPError(RuntimeError):
    """A CDP method returned an ``error`` object."""


def _free_port() -> int:
    """Grab a currently-free TCP port on loopback. Inherently racy, but the
    window between probe and Chrome binding it is small; a collision just fails
    the launch and the caller retries."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _chrome_bin() -> str:
    override = os.environ.get("RLM_CHROME_BIN", "").strip()
    if override:
        return override
    for name in _CHROME_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError(
        "no Chrome/Chromium binary found; set RLM_CHROME_BIN or install "
        "google-chrome-stable")


# ---------------------------------------------------------------------------
# Launch seam
# ---------------------------------------------------------------------------


class LaunchHandle:
    """Opaque result of a launch: how to reach the Chrome, and how to stop it.

    ``cdp_base`` is ``http://127.0.0.1:<port>``; ``stop()`` tears the engine
    down. Backends subclass to carry their own process/container reference.
    """

    def __init__(self, *, port: int, profile_dir: str, mode: str,
                 container_name: str = "", vnc_port: int = 0) -> None:
        self.port = port
        self.profile_dir = profile_dir
        self.mode = mode
        self.container_name = container_name
        self.vnc_port = vnc_port

    @property
    def cdp_base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def stop(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class LaunchBackend(Protocol):
    """Launch a Chrome, return a :class:`LaunchHandle` on ``127.0.0.1:<port>``.

    ``mode`` is ``"headless"`` (no display) or ``"rendered"`` (headful,
    watchable). ``port`` may be 0 to let the backend pick; it is echoed on the
    handle. (The ``attached`` mode does not launch — see :class:`AttachBackend`.)
    """

    name: str

    async def launch(self, *, mode: str, profile_dir: str,
                     port: int = 0) -> LaunchHandle: ...


class _HostHandle(LaunchHandle):
    def __init__(self, proc: subprocess.Popen, **kw: Any) -> None:
        super().__init__(**kw)
        self._proc = proc

    async def stop(self) -> None:
        proc = self._proc
        if proc.poll() is None:
            proc.terminate()
            try:
                await asyncio.to_thread(proc.wait, 10)
            except subprocess.TimeoutExpired:
                proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(proc.wait, 5)


class HostSubprocessBackend:
    """Launch Chrome as a plain host subprocess (T2; Docker-free).

    Headless mode is ``--headless=new``; rendered mode drops it (headful) and
    relies on a real ``DISPLAY`` / Xvfb being present — on a bare WSL host with
    no X server rendered mode is really the docker backend's job (T3), but the
    same flags work under an Xvfb the operator started.
    """

    name = "host"

    async def launch(self, *, mode: str, profile_dir: str,
                     port: int = 0) -> LaunchHandle:
        port = port or _free_port()
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        # A stale SingletonLock from a hard-killed prior Chrome blocks relaunch
        # on the same profile — clear it defensively.
        with contextlib.suppress(FileNotFoundError):
            (Path(profile_dir) / "SingletonLock").unlink()
        argv = [
            _chrome_bin(),
            f"--remote-debugging-port={port}",
            # Bind the debugging socket to loopback explicitly.
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-extensions",
            # /dev/shm is tiny in many sandboxes; keep Chrome off it.
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ]
        if mode != "rendered":
            argv.append("--headless=new")
        argv.append("about:blank")
        log.info("launching host chrome on :%d (mode=%s)", port, mode)
        proc = subprocess.Popen(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        handle = _HostHandle(proc, port=port, profile_dir=profile_dir, mode=mode)
        await _await_cdp_ready(handle.cdp_base)
        return handle


class _AttachHandle(LaunchHandle):
    """Handle for an *attached* (not launched) Chrome.

    ``cdp_base`` is the external endpoint verbatim (NOT forced to loopback), and
    ``stop()`` is a deliberate no-op: we borrow the user's browser, so releasing
    a session must never close it.
    """

    def __init__(self, *, cdp_base: str, **kw: Any) -> None:
        super().__init__(**kw)
        self._cdp_base = cdp_base

    @property
    def cdp_base(self) -> str:
        return self._cdp_base

    async def stop(self) -> None:
        return  # borrowed browser — never close it


class AttachBackend:
    """Attach to an EXISTING Chrome over CDP instead of launching one.

    Drives a real, already-running browser (e.g. the Windows desktop Chrome at
    ``RLM_ATTACH_CDP_URL``) — residential fingerprint + real logins, so it
    sidesteps the bot-detection a freshly-launched headless Chrome trips. It
    launches nothing; the returned handle's ``stop()`` is a no-op.
    """

    name = "attached"

    async def launch(self, *, mode: str, profile_dir: str, port: int = 0,
                     attach_url: str | None = None) -> LaunchHandle:
        url = (attach_url or _ATTACH_CDP_URL).strip().rstrip("/")
        # Verify the external Chrome is actually reachable before claiming it.
        await _await_cdp_ready(url, timeout=8.0)
        parsed = urlparse(url)
        log.info("attaching to existing chrome at %s", url)
        return _AttachHandle(
            cdp_base=url, port=parsed.port or 0, profile_dir="",
            mode="attached")


def _make_backend() -> LaunchBackend:
    """The *launch* backend (host/docker) for headless/rendered modes."""
    if _BACKEND == "docker":  # pragma: no cover - T3
        from awm.rlm_browser.docker_backend import DockerBackend
        return DockerBackend()
    return HostSubprocessBackend()


async def _await_cdp_ready(cdp_base: str, *, timeout: float = 20.0) -> None:
    """Poll ``/json/version`` until Chrome answers (or time out)."""
    deadline = asyncio.get_event_loop().time() + timeout
    async with httpx.AsyncClient(timeout=2.0) as cli:
        while True:
            try:
                r = await cli.get(f"{cdp_base}/json/version")
                if r.status_code == 200:
                    return
            except Exception:  # noqa: BLE001 - connection refused while booting
                pass
            if asyncio.get_event_loop().time() > deadline:
                raise RuntimeError(f"Chrome at {cdp_base} not ready in {timeout}s")
            await asyncio.sleep(0.15)


# ---------------------------------------------------------------------------
# CDP connection
# ---------------------------------------------------------------------------


class CDPConnection:
    """One DevTools websocket: id-matched request/reply + an event sink."""

    def __init__(self, ws: Any,
                 on_event: Callable[[dict], None] | None = None) -> None:
        self._ws = ws
        self._on_event = on_event
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None

    @classmethod
    async def connect(cls, ws_url: str,
                      on_event: Callable[[dict], None] | None = None
                      ) -> "CDPConnection":
        ws = await websockets.connect(ws_url, max_size=None, open_timeout=15)
        conn = cls(ws, on_event)
        conn._reader = asyncio.create_task(conn._read_loop())
        return conn

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                mid = msg.get("id")
                if mid is not None and mid in self._pending:
                    fut = self._pending.pop(mid)
                    if not fut.done():
                        fut.set_result(msg)
                elif msg.get("method") and self._on_event:
                    # Unsolicited protocol event (Page.loadEventFired, etc.).
                    with contextlib.suppress(Exception):
                        self._on_event(msg)
        except Exception as exc:  # noqa: BLE001 - ws closed under us
            log.debug("cdp read loop ended: %s", exc)
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(CDPError(f"connection closed: {exc}"))
            self._pending.clear()

    async def call(self, method: str, params: dict | None = None, *,
                   timeout: float = 30.0) -> Any:
        self._next_id += 1
        mid = self._next_id
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut
        await self._ws.send(json.dumps(
            {"id": mid, "method": method, "params": params or {}}))
        msg = await asyncio.wait_for(fut, timeout)
        if "error" in msg:
            raise CDPError(json.dumps(msg["error"]))
        return msg.get("result", {})

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self._reader
        with contextlib.suppress(Exception):
            await self._ws.close()


# ---------------------------------------------------------------------------
# Session + manager
# ---------------------------------------------------------------------------


class _Session:
    """In-process state for one acquired browser. Reconstructable from the DB
    row (cdp_port/mode) when the manager cache is cold after a restart."""

    def __init__(self, session_id: str, *, cdp_base: str, mode: str,
                 handle: LaunchHandle | None, borrowed: bool = False) -> None:
        self.session_id = session_id
        self.cdp_base = cdp_base
        self.mode = mode
        self.handle = handle  # None when lazily re-attached after a restart
        # Borrowed = an attached (not launched) Chrome we must never close.
        self.borrowed = borrowed
        self._browser_conn: CDPConnection | None = None
        self._tab_conns: dict[str, CDPConnection] = {}

    @property
    def ws_base(self) -> str:
        return self.cdp_base.replace("http://", "ws://")


class BrowserManager:
    """Owns every live session; implements the realm verbs over real CDP.

    Holds a back-reference to the :class:`ServiceAdapter` so protocol events can
    be relayed out as ``rlm.browser.<kind>`` emits (best-effort).
    """

    def __init__(self, emit: Callable[[str, Any], Awaitable[None]]) -> None:
        self._emit = emit
        # The launch backend (host/docker) realizes headless/rendered; the
        # attach backend realizes `attached` regardless of RLM_BROWSER_BACKEND.
        self._launch_backend = _make_backend()
        self._attach_backend = AttachBackend()
        self._sessions: dict[str, _Session] = {}
        self._lock = asyncio.Lock()
        # session_id -> peer name, for `attached` sessions forwarded to a peer
        # that owns the real Chrome (see hub_adapter._build()). In-memory only:
        # never persisted, since the owning peer's own DAO row is the durable
        # record — this is just local routing memory for this node's adapter.
        self._attach_forwards: dict[str, str] = {}

    def note_forwarded(self, session_id: str, peer: str) -> None:
        self._attach_forwards[session_id] = peer

    def forwarded_peer(self, session_id: str) -> str | None:
        return self._attach_forwards.get(session_id)

    def forget_forwarded(self, session_id: str) -> None:
        self._attach_forwards.pop(session_id, None)

    def _backend_for(self, mode: str) -> LaunchBackend:
        return self._attach_backend if mode == "attached" else self._launch_backend

    # -- session resolution ------------------------------------------------

    async def _resolve(self, session_id: str) -> _Session:
        """Return the live session, lazily re-attaching from the DB row if the
        cache is cold (post-restart)."""
        sess = self._sessions.get(session_id)
        if sess is not None:
            return sess
        row = await asyncio.to_thread(dao.BrowserDAO().get_session, session_id)
        if row is None:
            raise ValueError(f"unknown session_id: {session_id!r}")
        borrowed = (row.get("backend") == "attached")
        if borrowed:
            # Attached sessions live on an EXTERNAL host (not loopback) and the
            # row doesn't persist it — re-attach to the configured endpoint.
            cdp_base = _ATTACH_CDP_URL.rstrip("/")
        else:
            port = int(row.get("cdp_port") or 0)
            if not port:
                raise ValueError(
                    f"session {session_id!r} has no CDP port; re-acquire it")
            cdp_base = f"http://127.0.0.1:{port}"
        # Verify the Chrome is still alive before claiming the session.
        await _await_cdp_ready(cdp_base, timeout=5.0)
        sess = _Session(session_id, cdp_base=cdp_base,
                        mode=_norm_mode(row.get("mode")), handle=None,
                        borrowed=borrowed)
        self._sessions[session_id] = sess
        log.info("re-attached session %s at %s", session_id, cdp_base)
        return sess

    async def _browser_conn(self, sess: _Session) -> CDPConnection:
        if sess._browser_conn is None:
            async with httpx.AsyncClient(timeout=5.0) as cli:
                r = await cli.get(f"{sess.cdp_base}/json/version")
                r.raise_for_status()
                adv = r.json()["webSocketDebuggerUrl"]
            # Keep the advertised PATH (/devtools/browser/<id>) but dial it at the
            # session's reachable host:port — Chrome advertises the port it bound
            # internally (e.g. behind the container's socat bridge), which isn't
            # the published host port. Path is the only part that's authoritative.
            # Derive host:port from cdp_base (loopback for launched sessions, the
            # real external host for an attached one) — never hardcode loopback.
            pr = urlparse(sess.cdp_base)
            host = pr.hostname or "127.0.0.1"
            port = pr.port
            path = urlparse(adv).path
            ws_url = f"ws://{host}:{port}{path}"
            sess._browser_conn = await CDPConnection.connect(ws_url)
        return sess._browser_conn

    async def _tab_conn(self, sess: _Session, tab_id: str) -> CDPConnection:
        conn = sess._tab_conns.get(tab_id)
        if conn is not None:
            return conn
        ws_url = f"{sess.ws_base}/devtools/page/{tab_id}"
        conn = await CDPConnection.connect(
            ws_url, on_event=lambda m: self._on_tab_event(sess, tab_id, m))
        sess._tab_conns[tab_id] = conn
        return conn

    def _on_tab_event(self, sess: _Session, tab_id: str, msg: dict) -> None:
        """Relay a noteworthy CDP event out as rlm.browser.<kind>. Best-effort;
        most protocol events are noise, so only forward a curated few."""
        method = msg.get("method") or ""
        interesting = {
            "Page.loadEventFired": "page_loaded",
            "Page.javascriptDialogOpening": "dialog",
            "Inspector.targetCrashed": "crashed",
        }
        kind = interesting.get(method)
        if kind is None:
            return
        payload = {"session_id": sess.session_id, "tab_id": tab_id,
                   "kind": kind, "data": msg.get("params") or {}}
        # Fire-and-forget; emit is itself best-effort.
        with contextlib.suppress(RuntimeError):
            asyncio.create_task(self._emit("browser", payload))

    # -- lifecycle ---------------------------------------------------------

    async def acquire(self, game: str, mode: str | None = None,
                      opts: dict | None = None) -> dict:
        opts = opts or {}
        mode = _norm_mode(mode)
        egress = opts.get("egress")  # inert seam (T4)
        backend = self._backend_for(mode)
        profile = str(_PROFILES_DIR / (game.strip() or "default"))
        async with self._lock:
            if mode == "attached":
                # Singleton: one attached session at a time (one real window).
                actives = await asyncio.to_thread(
                    lambda: [s for s in dao.BrowserDAO().list_sessions()
                             if s.get("backend") == "attached"
                             and s.get("status") == "active"])
                if actives:
                    raise ValueError(
                        f"an attached session is already active "
                        f"({actives[0]['session_id']}); release it first — "
                        f"attached mode is single-window")
                handle = await backend.launch(
                    mode=mode, profile_dir=profile,
                    attach_url=opts.get("attach_url"))
            else:
                handle = await backend.launch(mode=mode, profile_dir=profile)
            row = await asyncio.to_thread(
                lambda: dao.BrowserDAO().create_session(
                    game, mode=mode, backend=backend.name,
                    cdp_port=handle.port, user_data_dir=handle.profile_dir,
                    container_name=handle.container_name,
                    vnc_port=handle.vnc_port, egress=egress))
            sess = _Session(row["session_id"], cdp_base=handle.cdp_base,
                            mode=mode, handle=handle,
                            borrowed=(mode == "attached"))
            self._sessions[row["session_id"]] = sess
        return {"session_id": row["session_id"], "mode": mode,
                "backend": backend.name}

    async def release(self, session_id: str) -> dict:
        # Resolve first (lazily re-attaching a cold session post-restart) so we
        # always hold a live browser connection to ask Chrome to close itself —
        # otherwise a re-attached session, whose original launch handle is gone,
        # would orphan its Chrome on release.
        sess = None
        with contextlib.suppress(Exception):
            sess = await self._resolve(session_id)
        async with self._lock:
            self._sessions.pop(session_id, None)
            if sess is not None:
                await self._teardown(sess)
            removed = await asyncio.to_thread(
                dao.BrowserDAO().delete_session, session_id)
        return {"released": bool(removed)}

    async def _teardown(self, sess: _Session) -> None:
        # Ask Chrome to exit via CDP — backend-agnostic and works even when the
        # launch handle is gone (re-attached session after a service restart).
        # NEVER for a borrowed (attached) browser: that's the user's real Chrome,
        # so we only disconnect our own sockets and leave it running.
        if not sess.borrowed:
            with contextlib.suppress(Exception):
                browser = await self._browser_conn(sess)
                await browser.call("Browser.close", timeout=5.0)
        for conn in list(sess._tab_conns.values()):
            await conn.close()
        sess._tab_conns.clear()
        if sess._browser_conn is not None:
            await sess._browser_conn.close()
            sess._browser_conn = None
        # handle.stop() still matters for docker (stop+rm the container) and as a
        # backstop for host if Browser.close didn't take.
        if sess.handle is not None:
            with contextlib.suppress(Exception):
                await sess.handle.stop()

    async def reset(self, session_id: str) -> dict:
        """Close every tab but keep the browser + session slot, then open one
        fresh blank tab — a cheap 'clear state' that survives the session."""
        sess = await self._resolve(session_id)
        browser = await self._browser_conn(sess)
        # Borrowed (attached) Chrome: don't close the user's existing tabs — just
        # drop our own connections and open a fresh blank tab.
        if not sess.borrowed:
            targets = (await browser.call("Target.getTargets")).get(
                "targetInfos", [])
            for t in targets:
                if t.get("type") == "page":
                    with contextlib.suppress(CDPError):
                        await browser.call("Target.closeTarget",
                                           {"targetId": t["targetId"]})
        for conn in list(sess._tab_conns.values()):
            await conn.close()
        sess._tab_conns.clear()
        await browser.call("Target.createTarget", {"url": "about:blank"})
        await asyncio.to_thread(dao.BrowserDAO().set_status, session_id, "active")
        return {"session_id": session_id, "status": "active"}

    async def status(self, session_id: str | None = None) -> dict:
        d = dao.BrowserDAO()
        if session_id:
            rows = await asyncio.to_thread(d.get_session, session_id)
            rows = [rows] if rows else []
        else:
            rows = await asyncio.to_thread(d.list_sessions)
        out = []
        for row in rows:
            entry = dict(row)
            # Live tab count when the Chrome is reachable; best-effort.
            with contextlib.suppress(Exception):
                tabs = await self._tab_list(row["session_id"])
                entry["tabs"] = tabs["tabs"]
            out.append(entry)
        return {"sessions": out}

    # -- tabs --------------------------------------------------------------

    async def tab_open(self, session_id: str, url: str | None = None) -> dict:
        sess = await self._resolve(session_id)
        browser = await self._browser_conn(sess)
        res = await browser.call("Target.createTarget",
                                 {"url": url or "about:blank"})
        return {"tab_id": res["targetId"]}

    async def tab_close(self, session_id: str, tab_id: str) -> dict:
        sess = await self._resolve(session_id)
        browser = await self._browser_conn(sess)
        await browser.call("Target.closeTarget", {"targetId": tab_id})
        conn = sess._tab_conns.pop(tab_id, None)
        if conn is not None:
            await conn.close()
        return {"closed": True, "tab_id": tab_id}

    async def _tab_list(self, session_id: str) -> dict:
        sess = await self._resolve(session_id)
        browser = await self._browser_conn(sess)
        targets = (await browser.call("Target.getTargets")).get("targetInfos", [])
        tabs = [
            {"tab_id": t["targetId"], "url": t.get("url"),
             "title": t.get("title"), "attached": t.get("attached")}
            for t in targets if t.get("type") == "page"
        ]
        return {"tabs": tabs}

    async def tab_list(self, session_id: str) -> dict:
        return await self._tab_list(session_id)

    async def tab_activate(self, session_id: str, tab_id: str) -> dict:
        sess = await self._resolve(session_id)
        browser = await self._browser_conn(sess)
        await browser.call("Target.activateTarget", {"targetId": tab_id})
        return {"activated": True, "tab_id": tab_id}

    # -- a tab to act on ---------------------------------------------------

    async def _pick_tab(self, sess: _Session, tab_id: str | None) -> str:
        if tab_id:
            return tab_id
        # Default to the first page target.
        browser = await self._browser_conn(sess)
        targets = (await browser.call("Target.getTargets")).get("targetInfos", [])
        for t in targets:
            if t.get("type") == "page":
                return t["targetId"]
        # None yet — open one.
        res = await browser.call("Target.createTarget", {"url": "about:blank"})
        return res["targetId"]

    # -- perceive ----------------------------------------------------------

    async def observe(self, session_id: str, tab_id: str | None = None) -> dict:
        sess = await self._resolve(session_id)
        tid = await self._pick_tab(sess, tab_id)
        conn = await self._tab_conn(sess, tid)
        await conn.call("Runtime.enable")
        html = await conn.call("Runtime.evaluate", {
            "expression": "document.documentElement.outerHTML",
            "returnByValue": True})
        url = await conn.call("Runtime.evaluate", {
            "expression": "location.href", "returnByValue": True})
        title = await conn.call("Runtime.evaluate", {
            "expression": "document.title", "returnByValue": True})
        return {
            "tab_id": tid,
            "html": (html.get("result") or {}).get("value"),
            "url": (url.get("result") or {}).get("value"),
            "title": (title.get("result") or {}).get("value"),
        }

    async def screenshot(self, session_id: str, tab_id: str | None = None,
                         full_page: bool = False) -> dict:
        sess = await self._resolve(session_id)
        tid = await self._pick_tab(sess, tab_id)
        conn = await self._tab_conn(sess, tid)
        await conn.call("Page.enable")
        params: dict[str, Any] = {"format": "png"}
        if full_page:
            metrics = await conn.call("Page.getLayoutMetrics")
            css = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
            if css:
                params["clip"] = {
                    "x": 0, "y": 0,
                    "width": css.get("width", 0), "height": css.get("height", 0),
                    "scale": 1}
            params["captureBeyondViewport"] = True
        shot = await conn.call("Page.captureScreenshot", params)
        return {"tab_id": tid, "image": shot.get("data"), "format": "png"}

    # -- engine relay + discovery ------------------------------------------

    async def cdp(self, session_id: str, method: str,
                  tab_id: str | None = None, params: dict | None = None) -> dict:
        """Relay an arbitrary CDP method. Routed to the tab's connection when a
        ``tab_id`` is given (or a default page is chosen), else the browser-level
        connection. This single verb is the entire act surface."""
        sess = await self._resolve(session_id)
        if tab_id == "__browser__":
            conn = await self._browser_conn(sess)
        else:
            tid = await self._pick_tab(sess, tab_id)
            conn = await self._tab_conn(sess, tid)
        result = await conn.call(method, params or {})
        return {"result": result}

    async def commands(self, session_id: str | None = None) -> dict:
        """The running Chrome's live ``/json/protocol`` — the dynamic, engine-
        described command catalog. Uses the session's Chrome when given, else a
        throwaway probe browser."""
        if session_id:
            sess = await self._resolve(session_id)
            cdp_base = sess.cdp_base
            async with httpx.AsyncClient(timeout=10.0) as cli:
                r = await cli.get(f"{cdp_base}/json/protocol")
                r.raise_for_status()
                return {"protocol": r.json()}
        # No session: spin a short-lived probe Chrome just to read its protocol.
        # Always the launch backend (never attach — we don't touch the user's
        # browser for a throwaway probe).
        handle = await self._launch_backend.launch(
            mode="headless", profile_dir=str(_PROFILES_DIR / "__probe__"))
        try:
            async with httpx.AsyncClient(timeout=10.0) as cli:
                r = await cli.get(f"{handle.cdp_base}/json/protocol")
                r.raise_for_status()
                return {"protocol": r.json()}
        finally:
            await handle.stop()

    async def shutdown(self) -> None:
        """Tear down every live session — called on service stop."""
        for sess in list(self._sessions.values()):
            with contextlib.suppress(Exception):
                await self._teardown(sess)
        self._sessions.clear()
