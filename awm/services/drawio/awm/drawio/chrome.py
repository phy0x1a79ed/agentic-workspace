"""Native SVG export: drawio's own renderer, driven in headless Chrome.

The containerized export server this service uses for PDF and PNG **cannot
produce SVG at all** — it answers ``400 Unsupported Format!``, because SVG in
drawio has always been a client-side operation: ``mxGraph.getSvg()`` walks the
live graph and serializes it. There is no server-side equivalent to call.

So for SVG we do what the export container does for every other format — load
drawio's own ``export3.html`` and let the real client render — and then take the
one extra step the container never takes: ask the rendered graph for its SVG.
The output is what the editor's *Export as SVG* produces: real ``<text>``
elements, a viewBox cropped to the drawing, ``data-cell-id`` on every shape.
Rasterizing to PNG or tracing a PDF would all produce *an* SVG; only this
produces a drawio SVG.

**Why the Chrome DevTools Protocol and not puppeteer/playwright.** Driving a
browser needs a browser and a wire protocol. The browser is already on the host,
and the wire protocol is JSON over a WebSocket — and this service already
depends on ``websockets`` and ``httpx`` for the gateway mount. A driver library
would add an install step and a browser download to buy nothing.

**Why the page comes from the gateway mount.** ``export3.html`` pulls in the
app bundle, stencils and fonts by relative path, and some of that travels over
XHR, which ``file://`` blocks. The gateway already serves the exact same
directory at ``/drawio-app`` for the editor, so we point Chrome at that and the
client loads the way it does for a person.

Images never travel: :func:`awm.drawio.export.inline_images` has already
replaced every ``/files/…`` reference with its bytes, so the page renders with
no network access to anything but its own assets.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx
from websockets.sync.client import connect as ws_connect

log = logging.getLogger("awm.drawio.chrome")

#: Candidates for the browser, in preference order. Overridable outright with
#: ``DRAWIO_CHROME`` when the host keeps it somewhere unusual.
CHROME_BINARIES = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "chrome",
)

#: How long to wait for the client to finish drawing. Generous: a large diagram
#: with many stencils and webfonts is slow the first time, and the cost of
#: giving up early is a published figure that never updates.
RENDER_TIMEOUT_S = 120.0

#: How long to wait for Chrome to come up and publish its debugging port.
LAUNCH_TIMEOUT_S = 30.0

#: How the "has it finished drawing?" poll backs off. Tight to begin with — on
#: a parked tab a small diagram is done in single-digit milliseconds and a fixed
#: 50 ms interval would be most of the render — then loose, so a large one does
#: not spend its wait on round trips.
POLL_START_S = 0.005
POLL_MAX_S = 0.05

#: Wipe the parked tab back to how it loaded.
#:
#: drawio's own ``render`` clears the body before it draws (``export.js``:
#: ``document.body.innerText = ''``), so what actually has to be undone is what
#: it leaves *outside* the body: the inline width/height/background it sets on
#: ``<body>``, and anything it appended to ``<head>`` — a MathJax stylesheet,
#: the shadow blocker. Both are restored from a snapshot taken before the client
#: ever ran rather than by enumerating what it might have added, so a client
#: upgrade that starts leaving something new behind cannot quietly poison the
#: next render. Returns whether the client is still there to drive.
_RESET_SCRIPT = """(function(){
    delete window.__awmGraph;
    if (window.__awmRealGraph) { window.Graph = window.__awmRealGraph; }
    document.body.innerText = '';
    document.body.setAttribute('style', window.__awmBodyStyle);
    var head = document.head;
    while (head.childElementCount > window.__awmHeadLength) {
        head.removeChild(head.lastElementChild);
    }
    return typeof render === 'function';
})()"""

#: Taken once, immediately after the client loads and before it has drawn.
_SNAPSHOT_SCRIPT = """(function(){
    window.__awmBodyStyle = document.body.getAttribute('style') || '';
    window.__awmHeadLength = document.head.childElementCount;
})()"""


class ChromeError(RuntimeError):
    """The browser could not be started, reached, or made to render."""


#: Ask the drawn graph for its SVG, and — when a crop frame was named — trim the
#: result to that frame's box and delete the frame itself. ``__CROP_ID__`` is
#: substituted with a JSON string or ``null``.
_SVG_SCRIPT = """(function(){
    var g = window.__awmGraph;
    var bg = g.background;
    if (bg == mxConstants.NONE) { bg = null; }
    var done = document.getElementById('LoadingComplete');
    var scale = parseFloat(done.getAttribute('scale')) || 1;
    var border = parseInt(done.getAttribute('border')) || 0;
    var root = g.getSvg(bg, scale, border, false, null, true, null,
                        null, null, null, null, 'auto');
    if (g.shadowVisible) { g.addSvgShadow(root); }
    if (g.mathEnabled && typeof Editor !== 'undefined'
        && Editor.prototype.addMathCss) {
        Editor.prototype.addMathCss(root);
    }

    var cropId = __CROP_ID__;
    if (cropId != null) {
        document.body.appendChild(root);
        try {
            var frame = root.querySelector('[data-cell-id="' + cropId + '"]');
            if (frame == null) {
                throw new Error('the crop frame ' + cropId + ' drew nothing to '
                    + 'measure; give the shape a size and no children');
            }
            var m = root.getScreenCTM().inverse().multiply(frame.getScreenCTM());
            var b = frame.getBBox();
            var xs = [], ys = [];
            var corners = [[b.x, b.y], [b.x + b.width, b.y],
                           [b.x, b.y + b.height], [b.x + b.width, b.y + b.height]];
            for (var i = 0; i < corners.length; i++) {
                var p = root.createSVGPoint();
                p.x = corners[i][0]; p.y = corners[i][1];
                var q = p.matrixTransform(m);
                xs.push(q.x); ys.push(q.y);
            }
            var x = Math.min.apply(null, xs), y = Math.min.apply(null, ys);
            var w = Math.max.apply(null, xs) - x, h = Math.max.apply(null, ys) - y;
            if (!(w > 0) || !(h > 0)) {
                throw new Error('the crop frame ' + cropId + ' has no area');
            }
            var r = function(v) { return Math.round(v * 100) / 100; };
            x = r(x); y = r(y); w = r(w); h = r(h);
            frame.parentNode.removeChild(frame);
            root.setAttribute('viewBox', x + ' ' + y + ' ' + w + ' ' + h);
            root.setAttribute('width', w + 'px');
            root.setAttribute('height', h + 'px');
        } finally {
            if (root.parentNode != null) { root.parentNode.removeChild(root); }
        }
    }

    return new XMLSerializer().serializeToString(root);
})()"""


def chrome_binary() -> str | None:
    override = os.environ.get("DRAWIO_CHROME")
    if override:
        return override if Path(override).is_file() else None
    for name in CHROME_BINARIES:
        found = shutil.which(name)
        if found:
            return found
    return None


def export_page_url() -> str:
    """Where to load drawio's export client from.

    The gateway's ``/drawio-app`` mount by preference — it is the same directory
    the editor is served from, over http, so relative assets and XHR both work.
    ``DRAWIO_EXPORT_PAGE`` overrides it outright.
    """
    override = os.environ.get("DRAWIO_EXPORT_PAGE")
    if override:
        return override
    from . import mount

    hub = os.environ.get("AWM_HUB_URL", "").rstrip("/")
    if not hub:
        raise ChromeError(
            "AWM_HUB_URL is not set, so the drawio client cannot be loaded over "
            "http; set DRAWIO_EXPORT_PAGE to an export3.html URL to override"
        )
    return f"{hub}{mount.MOUNT_PREFIX}/export3.html"


# --- the Chrome DevTools Protocol, minimally ------------------------------

class _Session:
    """One CDP connection with one attached page target.

    Deliberately tiny: create a target, attach to it, evaluate expressions,
    close. Everything that makes a general CDP client complicated — event
    subscriptions, multiple sessions, domains we never enable — is absent.
    """

    def __init__(self, ws, session_id: str, target_id: str):
        self.ws = ws
        self.session_id = session_id
        self.target_id = target_id
        self._next_id = 0

    def send(self, method: str, params: dict | None = None,
             scoped: bool = True) -> dict:
        self._next_id += 1
        message = {"id": self._next_id, "method": method, "params": params or {}}
        if scoped:
            message["sessionId"] = self.session_id
        self.ws.send(json.dumps(message))
        deadline = time.monotonic() + RENDER_TIMEOUT_S
        while time.monotonic() < deadline:
            raw = self.ws.recv(timeout=max(1.0, deadline - time.monotonic()))
            reply = json.loads(raw)
            # Events and replies share the socket; anything without our id is
            # a notification we did not ask for.
            if reply.get("id") != self._next_id:
                continue
            if "error" in reply:
                raise ChromeError(f"{method}: {reply['error'].get('message')}")
            return reply.get("result", {})
        raise ChromeError(f"{method}: no reply within {RENDER_TIMEOUT_S:.0f}s")

    def evaluate(self, expression: str):
        """Evaluate JS and return its value.

        Every expression here must yield a *primitive*. Returning a DOM node or
        the graph itself makes CDP try to serialize the whole object graph and
        fail with "Object reference chain is too long", which is why the render
        call below is wrapped to return nothing.
        """
        result = self.send("Runtime.evaluate", {
            "expression": expression, "returnByValue": True,
            "awaitPromise": False,
        })
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            message = (detail.get("exception", {}).get("description")
                       or detail.get("text") or "unknown error")
            raise ChromeError(f"page script failed: {str(message)[:400]}")
        return result.get("result", {}).get("value")


class Browser:
    """A headless Chrome kept alive between renders.

    Relaunching per export would cost a second or more each time, and
    autopublish can render every few seconds while somebody edits. The process
    is deliberately **not** put in its own session: staying in the service's
    process group means the hub supervisor's group kill takes the browser down
    with the service instead of orphaning it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._ws_url: str | None = None
        self._profile: Path | None = None
        #: The parked export tab and the socket driving it. One render's worth
        #: of setup, kept across all of them.
        self._tab: _Session | None = None
        self._ws = None
        self._tab_url: str | None = None

    # -- lifecycle ---------------------------------------------------------

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _launch(self) -> None:
        binary = chrome_binary()
        if not binary:
            raise ChromeError(
                "no Chrome/Chromium on PATH; SVG export needs a browser because "
                "drawio renders SVG client-side (set DRAWIO_CHROME to override)"
            )
        self._profile = Path(tempfile.mkdtemp(prefix="awm-drawio-chrome-"))
        self._proc = subprocess.Popen(
            [binary, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--no-first-run", "--no-default-browser-check",
             "--disable-dev-shm-usage", "--hide-scrollbars",
             "--remote-debugging-port=0",
             f"--user-data-dir={self._profile}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        # Port 0 means "pick one and tell me": Chrome writes the chosen port to
        # DevToolsActivePort once it is listening. Reading that beats guessing a
        # fixed port, which would collide with a second sandbox on this host.
        port_file = self._profile / "DevToolsActivePort"
        deadline = time.monotonic() + LAUNCH_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise ChromeError(
                    f"chrome exited immediately (rc={self._proc.returncode})")
            if port_file.is_file():
                text = port_file.read_text(encoding="utf-8")
                if "\n" in text:
                    port = int(text.splitlines()[0])
                    self._ws_url = self._browser_ws(port)
                    log.info("headless chrome up for SVG export (pid=%d, "
                             "devtools port %d)", self._proc.pid, port)
                    return
            time.sleep(0.05)
        self.close()
        raise ChromeError(f"chrome did not start within {LAUNCH_TIMEOUT_S:.0f}s")

    @staticmethod
    def _browser_ws(port: int) -> str:
        with httpx.Client(timeout=10) as client:
            version = client.get(f"http://127.0.0.1:{port}/json/version").json()
        return version["webSocketDebuggerUrl"]

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        self._discard_tab()
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc, self._ws_url = None, None
        if self._profile is not None:
            shutil.rmtree(self._profile, ignore_errors=True)
            self._profile = None

    def state(self) -> str:
        """``running`` / ``stopped`` / ``no-chrome`` — for the status verb."""
        if self._alive():
            return "running"
        return "stopped" if chrome_binary() else "no-chrome"

    # -- the parked export tab ---------------------------------------------
    #
    # Every render used to create a target, navigate it to ``export3.html`` and
    # throw it away — which meant re-fetching and re-executing drawio's whole
    # app bundle each time, for a page that then drew for a few milliseconds.
    # One tab is kept parked on that page instead and reset between renders.
    #
    # Reuse is never load-bearing: any anomaly — the tab gone, the reset
    # failing, the render not answering — discards it and falls back to the
    # create-navigate-render path, so this can speed the service up but cannot
    # wedge it into a state only a restart leaves.

    def _discard_tab(self) -> None:
        """Throw the parked tab away. Never raises — this *is* the fallback."""
        session, ws = self._tab, self._ws
        self._tab, self._ws, self._tab_url = None, None, None
        if session is not None:
            try:
                session.send("Target.closeTarget",
                             {"targetId": session.target_id}, scoped=False)
            except Exception:  # noqa: BLE001
                log.debug("could not close the parked tab", exc_info=True)
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                log.debug("could not close the parked tab's socket",
                          exc_info=True)

    def _park(self, url: str) -> _Session:
        """Create a tab, load drawio's export client into it, and keep it."""
        ws = ws_connect(self._ws_url, max_size=None,
                        open_timeout=15)  # type: ignore[arg-type]
        session = _Session(ws, "", "")
        try:
            target = session.send("Target.createTarget", {"url": "about:blank"},
                                  scoped=False)
            session.target_id = target["targetId"]
            attached = session.send(
                "Target.attachToTarget",
                {"targetId": session.target_id, "flatten": True}, scoped=False)
            session.session_id = attached["sessionId"]
            session.send("Page.enable")
            session.send("Runtime.enable")
            session.send("Page.navigate", {"url": url})

            deadline = time.monotonic() + RENDER_TIMEOUT_S
            while time.monotonic() < deadline:
                if session.evaluate("typeof render") == "function":
                    break
                time.sleep(POLL_MAX_S)
            else:
                raise ChromeError(
                    f"drawio's export client never loaded from {url}; is the "
                    "gateway's /drawio-app mount up?")
            session.evaluate(_SNAPSHOT_SCRIPT)
        except BaseException:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
            raise
        self._ws, self._tab, self._tab_url = ws, session, url
        return session

    def _reusable(self, url: str) -> "_Session | None":
        """The parked tab, wiped back to how it loaded — or ``None`` to rebuild."""
        session = self._tab
        if session is None or self._tab_url != url:
            if session is not None:
                self._discard_tab()
            return None
        try:
            ready = session.evaluate(_RESET_SCRIPT)
        except Exception as exc:  # noqa: BLE001 — any anomaly means rebuild
            log.debug("the parked export tab did not reset (%s)", exc)
            ready = False
        if not ready:
            self._discard_tab()
            return None
        return session

    # -- rendering ---------------------------------------------------------

    def render_svg(self, xml: str, *, scale: float = 1.0,
                   page: int | None = None, border: int = 0,
                   transparent: bool = True, crop_id: str | None = None) -> str:
        """Render one diagram to drawio's own SVG. Blocking.

        Serialized with a lock: one browser, and two concurrent renders would
        interleave their targets' setup for no gain, since the work is the
        page's, not ours.

        The lock is **not** re-entrant, so nothing reached from here may render
        again. Embedded page views are resolved by
        :func:`awm.drawio.export.inline_images`, which runs strictly before this
        call; moving inlining inside it would self-deadlock.
        """
        with self._lock:
            if not self._alive():
                self._close_locked()
                self._launch()
            try:
                return self._render_locked(xml, scale, page, border, transparent,
                                           crop_id)
            except Exception as exc:  # noqa: BLE001 — every failure is fatal here
                # A browser that failed once is suspect — a crashed renderer
                # would otherwise fail every future publish identically. Drop
                # it so the next call starts clean. Deliberately every
                # exception, not just ``ChromeError``: the socket outlives a
                # render now, so a closed connection surfaces as the websocket
                # library's own error and must not leave a dead browser parked.
                log.warning("svg render failed (%s); restarting headless chrome",
                            exc)
                self._close_locked()
                raise

    def _render_locked(self, xml: str, scale: float, page: int | None,
                       border: int, transparent: bool,
                       crop_id: str | None = None) -> str:
        url = export_page_url()
        session = self._reusable(url)
        if session is not None:
            try:
                return self._render_in_target(session, xml, scale, page,
                                              border, transparent, crop_id)
            except Exception as exc:  # noqa: BLE001 — one retry, then honest
                # The retry costs a second render of a diagram that may simply
                # be unrenderable. That is the price of never leaving a wedged
                # tab in place, and it is paid once.
                log.warning("the parked export tab failed to render (%s); "
                            "falling back to a fresh one", exc)
                self._discard_tab()
        session = self._park(url)
        return self._render_in_target(session, xml, scale, page, border,
                                      transparent, crop_id)

    def _render_in_target(self, session: _Session, xml: str,
                          scale: float, page: int | None, border: int,
                          transparent: bool, crop_id: str | None = None) -> str:
        deadline = time.monotonic() + RENDER_TIMEOUT_S

        # Capture the Graph the client is about to build. `render` keeps it in a
        # closure, so the only handle is the constructor it calls — wrap it,
        # then put the original back immediately, since the wrapper is a lie
        # that only has to survive one `new`.
        session.evaluate(
            "(function(){var G=window.Graph;"
            "function W(c){var g=new G(c);window.__awmGraph=g;return g;}"
            "W.prototype=G.prototype;Object.setPrototypeOf(W,G);"
            "window.__awmRealGraph=G;window.Graph=W;})()"
        )

        arg: dict = {
            "xml": xml,
            # 'png' rather than 'svg': this is the client's *layout* mode, and
            # it has no svg branch — svg comes from getSvg below, after the
            # client has drawn. Asking for a format it does not know would
            # change how it lays the page out.
            "format": "png",
            "scale": scale,
            "border": border,
            "crop": "1",           # bound the drawing, not the paper size
            "bg": "none" if transparent else None,
        }
        if page is not None:
            arg["from"] = arg["to"] = int(page)
        arg = {k: v for k, v in arg.items() if v is not None}

        # Wrapped to return nothing: render's return value is a live object
        # graph and CDP refuses to serialize it.
        session.evaluate(f"(function(){{render({json.dumps(arg)});}})()")
        session.evaluate("window.Graph=window.__awmRealGraph;")

        interval = POLL_START_S
        while time.monotonic() < deadline:
            if session.evaluate("!!document.getElementById('LoadingComplete')"):
                break
            time.sleep(interval)
            interval = min(interval * 2, POLL_MAX_S)
        else:
            raise ChromeError(
                "drawio's client did not finish drawing within "
                f"{RENDER_TIMEOUT_S:.0f}s")

        if not session.evaluate("!!window.__awmGraph"):
            raise ChromeError(
                "drawio's client drew without constructing a Graph we could "
                "capture — the export client's shape changed")

        # The same call drawio's own Electron export makes (see the
        # 'get-svg-data' handler in the client's js/export.js): background,
        # the scale the client actually used, our border, and links left alone.
        #
        # Cropping is measured, not computed. The exported SVG carries
        # `data-cell-id` on every shape group (drawio's own
        # createSvgImageExport puts it there), so the frame can be found in the
        # output and its box read straight off the DOM — via the CTM relative to
        # the root, which cancels device pixel ratio, scroll and every transform
        # in between. Reconstructing getSvg's translate/scale arithmetic in
        # Python would be the same answer derived from assumptions that a client
        # upgrade is free to break.
        svg = session.evaluate(_SVG_SCRIPT.replace(
            "__CROP_ID__", json.dumps(crop_id)))

        if not svg or "<svg" not in svg:
            raise ChromeError("drawio's client returned no SVG")
        if not svg.startswith("<?xml"):
            svg = '<?xml version="1.0" encoding="UTF-8"?>\n' + svg
        return svg


#: One browser per service process, started on first use.
BROWSER = Browser()


def render_svg(xml: str, *, scale: float = 1.0, page: int | None = None,
               border: int = 0, crop_id: str | None = None) -> str:
    return BROWSER.render_svg(xml, scale=scale, page=page, border=border,
                              crop_id=crop_id)


def state() -> str:
    return BROWSER.state()
