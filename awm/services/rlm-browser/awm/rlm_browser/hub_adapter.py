"""Hub adapter for the rlm-browser realm service.

Boots rlm-browser as a gateway-registered process: stands up its own DB, then
runs the shared :class:`awm.gatewayclient.ServiceAdapter` loop (register → ready
→ serve → reconnect). The functions below are exposed over the control WS and
projected into the gateway catalog as ``rlm_browser_<verb>`` tools.

Contract shape — *relay + live introspection*, deliberately NOT a hand-authored
browser command set:

  - A small fixed core stays ours: the realm lifecycle (acquire with a 3-valued
    ``mode`` — headless/rendered/attached — release, reset, status), tab
    management, and the two perceive conveniences (``observe`` = rendered HTML,
    ``screenshot`` = image).
  - The full act surface is a SINGLE verb, ``cdp``, which relays any DevTools
    method straight to the engine. We do not enumerate navigate/click/type —
    they are reached through ``cdp`` (e.g. ``Page.navigate``,
    ``Input.dispatchKeyEvent``).
  - The command catalog is DISCOVERED, not declared: ``commands`` returns the
    running Chrome's own ``/json/protocol`` self-description. Swapping the
    backend (simple↔rendered, host↔docker) changes none of these signatures.

The browser itself lives in :mod:`awm.rlm_browser.browser` (raw CDP over a
swappable launch seam). This module is just the manifest + thin arg-unpacking
handlers bound to one :class:`BrowserManager`.

Run via ``run.sh`` (which the hub spawns and respawns):
    python -m awm.rlm_browser.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter
from awm.rlm_browser import dao
from awm.rlm_browser.browser import BrowserManager

log = logging.getLogger("awm.rlm_browser.hub_adapter")


def _mode_from_args(a: dict) -> str | None:
    """Resolve the requested acquire mode, honoring the deprecated boolean
    ``graphics`` alias (true→rendered, false→headless). Returns ``None`` when
    neither is given, so the manager applies its own default."""
    mode = a.get("mode")
    if mode is None and "graphics" in a:
        return "rendered" if a.get("graphics") else "headless"
    return mode

API_MANIFEST: dict[str, Any] = {
    "functions": [
        # ---- lifecycle ----
        {
            "name": "acquire",
            "tool": "rlm_browser_acquire",
            "description": (
                "Acquire a browser realm session for a game; returns "
                "{session_id, mode, backend}. mode (default 'headless') is one "
                "of: 'headless' (launch Chrome, no display — fast, for bots at "
                "scale); 'rendered' (launch headful + VNC — watchable live); "
                "'attached' (do NOT launch — drive an EXISTING real Chrome over "
                "CDP, e.g. the desktop browser; borrowed, so release never "
                "closes it, and only one attached session may be active at a "
                "time). mode is fixed for the session's life. (Legacy: a boolean "
                "'graphics' is still accepted — true→rendered, false→headless.)"
            ),
            "params": [
                {"name": "game", "type": "string", "required": True},
                {"name": "mode", "type": "string", "required": False},
                {"name": "opts", "type": "object", "required": False},
            ],
        },
        {
            "name": "release",
            "tool": "rlm_browser_release",
            "description": "Release a session: tear its Chrome down and drop the row.",
            "params": [
                {"name": "session_id", "type": "string", "required": True},
            ],
        },
        {
            "name": "reset",
            "tool": "rlm_browser_reset",
            "description": (
                "Reset a session in place: close every tab, keep the browser + "
                "slot, open one fresh blank tab."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
            ],
        },
        {
            "name": "status",
            "tool": "rlm_browser_status",
            "description": (
                "Status of one session (pass session_id) or all (omit it). "
                "Returns {sessions: [{session_id, game, mode, backend, egress, "
                "tabs, ...}]}; egress is the inert VPN seam."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": False},
            ],
        },
        # ---- tabs ----
        {
            "name": "tab_open",
            "tool": "rlm_browser_tab_open",
            "description": "Open a new tab (optionally at url). Returns {tab_id}.",
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "url", "type": "string", "required": False},
            ],
        },
        {
            "name": "tab_close",
            "tool": "rlm_browser_tab_close",
            "description": "Close a tab by tab_id.",
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "tab_id", "type": "string", "required": True},
            ],
        },
        {
            "name": "tab_list",
            "tool": "rlm_browser_tab_list",
            "description": "List the session's live tabs. Returns {tabs: [...]}.",
            "params": [
                {"name": "session_id", "type": "string", "required": True},
            ],
        },
        {
            "name": "tab_activate",
            "tool": "rlm_browser_tab_activate",
            "description": "Bring a tab to the foreground by tab_id.",
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "tab_id", "type": "string", "required": True},
            ],
        },
        # ---- perceive (conveniences over CDP) ----
        {
            "name": "observe",
            "tool": "rlm_browser_observe",
            "description": (
                "Read a tab's RENDERED page (post-JS DOM). Returns "
                "{tab_id, html, url, title}. Omit tab_id for the active page."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "tab_id", "type": "string", "required": False},
            ],
        },
        {
            "name": "screenshot",
            "tool": "rlm_browser_screenshot",
            "description": (
                "Capture a PNG of a tab. Returns {tab_id, image (base64), "
                "format}. full_page=true captures beyond the viewport."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "tab_id", "type": "string", "required": False},
                {"name": "full_page", "type": "boolean", "required": False},
            ],
        },
        # ---- engine relay (THE full act set) ----
        {
            "name": "cdp",
            "tool": "rlm_browser_cdp",
            "description": (
                "Relay an arbitrary Chrome DevTools Protocol method to the "
                "engine and return {result}. This single verb IS the full act "
                "surface (e.g. method='Page.navigate' params={url}; "
                "method='Runtime.evaluate' params={expression,returnByValue}). "
                "Routed to the tab when tab_id is given, else a default page; "
                "pass tab_id='__browser__' for browser-level methods. Discover "
                "available methods via the 'commands' verb."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "method", "type": "string", "required": True},
                {"name": "tab_id", "type": "string", "required": False},
                {"name": "params", "type": "object", "required": False},
            ],
        },
        # ---- discovery (live, from the engine) ----
        {
            "name": "commands",
            "tool": "rlm_browser_commands",
            "description": (
                "Return the running Chrome's LIVE CDP protocol descriptor "
                "(/json/protocol): every domain/method/param available through "
                "the 'cdp' relay. Pass session_id to read that session's engine, "
                "or omit it for a throwaway probe browser. This is the dynamic, "
                "discoverable command catalog — not a hardcoded copy."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": False},
            ],
        },
    ],
    "emitters": [
        {
            "topic": "browser",
            "description": (
                "Fires on a realm-side browser event. Payload "
                "{session_id, tab_id, kind, data} where kind is e.g. "
                "'page_loaded', 'dialog', or 'crashed' — projected as "
                "rlm.browser.<kind>. Effectors subscribe filtered to their "
                "session_id. Best-effort live signalling, not durable delivery."
            ),
        },
    ],
    "sessions": [],
}


def _build() -> tuple[ServiceAdapter, BrowserManager]:
    """Wire the manager and adapter together. The manager needs the adapter's
    ``emit`` (to relay browser events) while the adapter needs the handlers at
    construction — break the cycle with a holder closure."""
    holder: dict[str, ServiceAdapter] = {}

    async def emit(topic: str, payload: Any) -> None:
        adapter = holder.get("adapter")
        if adapter is not None:
            await adapter.emit(topic, payload)

    mgr = BrowserManager(emit)

    # Async wrappers (NOT lambdas): the adapter checks
    # ``inspect.iscoroutinefunction`` and awaits these directly on its loop —
    # a lambda would not register as a coroutine fn and would needlessly hop a
    # worker thread just to build the coroutine.
    async def acquire(a):
        return await mgr.acquire(
            a["game"], mode=_mode_from_args(a), opts=a.get("opts"))

    async def release(a):
        return await mgr.release(a["session_id"])

    async def reset(a):
        return await mgr.reset(a["session_id"])

    async def status(a):
        return await mgr.status(a.get("session_id"))

    async def tab_open(a):
        return await mgr.tab_open(a["session_id"], a.get("url"))

    async def tab_close(a):
        return await mgr.tab_close(a["session_id"], a["tab_id"])

    async def tab_list(a):
        return await mgr.tab_list(a["session_id"])

    async def tab_activate(a):
        return await mgr.tab_activate(a["session_id"], a["tab_id"])

    async def observe(a):
        return await mgr.observe(a["session_id"], a.get("tab_id"))

    async def screenshot(a):
        return await mgr.screenshot(
            a["session_id"], a.get("tab_id"),
            full_page=bool(a.get("full_page", False)))

    async def cdp(a):
        return await mgr.cdp(
            a["session_id"], a["method"], tab_id=a.get("tab_id"),
            params=a.get("params"))

    async def commands(a):
        return await mgr.commands(a.get("session_id"))

    handlers = {
        "acquire": acquire, "release": release, "reset": reset,
        "status": status, "tab_open": tab_open, "tab_close": tab_close,
        "tab_list": tab_list, "tab_activate": tab_activate,
        "observe": observe, "screenshot": screenshot, "cdp": cdp,
        "commands": commands,
    }

    adapter = ServiceAdapter(
        "rlm-browser", API_MANIFEST, handlers, on_start=dao.init)
    holder["adapter"] = adapter
    return adapter, mgr


# Exposed for tests / introspection (the live instances are built in main()).
HANDLERS_DOC = (
    "acquire release reset status tab_open tab_close tab_list tab_activate "
    "observe screenshot cdp commands")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    adapter, _mgr = _build()
    await adapter.run()


if __name__ == "__main__":
    asyncio.run(main())
