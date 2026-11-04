"""The ``browser`` singleton's verbs (fleet.md R102)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Callable

from awm.browser_seats.seats import SeatBook, SeatError

TOOL = "browser"


def _p(name: str, type_: str = "string", required: bool = False, desc: str = "") -> dict:
    return {"name": name, "type": type_, "required": required, "description": desc}


SEAT = _p("seat", required=True, desc="The seat id that seat_open returned.")
TAB = _p("tab", required=True, desc="A tab id of this seat's window.")

VERBS: list[dict[str, Any]] = [
    {"name": "seat_open", "effect": "write", "description": "Attach this session to its own browser window. A trusted "
     "session gets Tony's profile unless it asks for a blank one. A cooperative session always gets "
     "a blank one. Returns the seat id.",
     "params": [_p("profile", desc="tony or blank."),
                _p("own_container", "boolean", desc="Run this seat in its own container.")]},
    {"name": "seat_close", "effect": "write", "description": "Close this seat's window, tabs and container.", "params": [SEAT]},
    {"name": "seats", "effect": "read", "description": "This session's seats.", "params": []},
    {"name": "tab_open", "effect": "write", "description": "Open a tab in this seat's window.",
     "params": [SEAT, _p("url", required=True)]},
    {"name": "tab_close", "effect": "write", "description": "Close a tab of this seat.", "params": [SEAT, TAB]},
    {"name": "tab_activate", "effect": "write", "description": "Switch to a tab of this seat.", "params": [SEAT, TAB]},
    {"name": "tab_list", "effect": "read", "description": "This seat's tabs.", "params": [SEAT]},
    {"name": "navigate", "effect": "write", "description": "Load a URL in a tab of this seat.",
     "params": [SEAT, TAB, _p("url", required=True)]},
    {"name": "cdp", "effect": "write", "description": "Run a page-level DevTools method on a tab of this seat. Target, "
     "Browser, Storage and cookie-wide methods are refused.",
     "params": [SEAT, TAB, _p("method", required=True), _p("params", "object")]},
    {"name": "screenshot", "effect": "read", "description": "A PNG of a tab, base64.", "params": [SEAT, TAB]},
    {"name": "signin", "effect": "write", "description": "Ask Tony to pass a captcha or sign in on this seat's window. "
     "Files a request on the approvals queue and raises the window in the viewer.",
     "params": [SEAT, _p("site", required=True), _p("reason", required=True)]},
    {"name": "viewer", "effect": "read", "description": "The one viewer that shows every window.", "params": []},
]

API_MANIFEST = {
    "description": "alcor's browser (fleet.md R102): each session drives its own Chrome window, "
                   "its seat. One viewer shows them all.",
    "functions": [{**v, "tool": f"{TOOL}_{v['name']}"} for v in VERBS],
    "emitters": [], "sessions": [],
}


def caller(a: dict) -> tuple[str, str]:
    """The calling session and its trust level, as the gateway passed them.

    A call that names no level is untrusted: the gateway stamps every call it routes.
    """
    claim = a.get("_caller_session")
    session = str(claim.get("job") or claim.get("name") or "") if isinstance(claim, dict) else str(claim or "")
    level = str(a.get("_caller_trust") or "untrusted")
    return session, level


def handlers(book: SeatBook, *, node: str, viewer_url: str,
             file_request: Callable[..., str] | None = None) -> dict[str, Callable[[dict], Any]]:
    def guard(fn):
        async def run(a: dict) -> Any:
            try:
                return await fn(a, *caller(a))
            except SeatError as exc:
                return {"error": str(exc)}
        return run

    async def seat_open(a, session, level):
        seat = await book.open(session, level, profile=a.get("profile"),
                               own_container=bool(a.get("own_container")))
        return seat.view()

    async def seat_close(a, session, level):
        await book.close(a["seat"], session)
        return {"closed": a["seat"]}

    async def tab_open(a, session, level):
        return {"tab": await book.tab_open(a["seat"], session, a["url"])}

    async def tab_close(a, session, level):
        await book.tab_close(a["seat"], session, a["tab"])
        return {"closed": a["tab"]}

    async def tab_activate(a, session, level):
        await book.tab_activate(a["seat"], session, a["tab"])
        return {"active": a["tab"]}

    async def signin(a, session, level):
        book._seat(a["seat"], session)
        if file_request is None:
            raise SeatError("no approvals queue is configured")
        rid = await asyncio.to_thread(file_request, "browser_signin", node=node, reason=a["reason"],
                                      payload={"seat": a["seat"], "site": a["site"]})
        await book.raise_window(a["seat"], session)
        return {"request_id": rid, "viewer": viewer_url}

    async def screenshot(a, session, level):
        return await book.cdp(a["seat"], session, a["tab"], "Page.captureScreenshot", {"format": "png"})

    return {
        "seat_open": guard(seat_open),
        "seat_close": guard(seat_close),
        "seats": guard(lambda a, s, l: _done(book.list(s))),
        "tab_open": guard(tab_open),
        "tab_close": guard(tab_close),
        "tab_activate": guard(tab_activate),
        "tab_list": guard(lambda a, s, l: book.tab_list(a["seat"], s)),
        "navigate": guard(lambda a, s, l: book.navigate(a["seat"], s, a["tab"], a["url"])),
        "cdp": guard(lambda a, s, l: book.cdp(a["seat"], s, a["tab"], a["method"], a.get("params"))),
        "screenshot": guard(screenshot),
        "signin": guard(signin),
        "viewer": guard(lambda a, s, l: _done({"viewer": viewer_url})),
    }


async def _done(v: Any) -> Any:
    return v


def settings() -> dict[str, Any]:
    d = Path(os.environ.get("AWM_DIR", Path.home() / ".awm"))
    cfg_file = d / "browser.json"
    cfg = json.loads(cfg_file.read_text()) if cfg_file.exists() else {}
    return {
        "dir": d,
        "cdp": cfg.get("cdp", "http://127.0.0.1:9222"),
        "viewer": cfg.get("viewer", ""),
        "service_sites": cfg.get("service_sites", {}),
        "seat_image": cfg.get("seat_image", "awm-browser-seat:latest"),
        "tony_profile": cfg.get("tony_profile"),
    }


async def main() -> None:
    from awm import gatewayclient
    from awm.browser_seats.cdp import Chrome
    from awm.browser_seats.containers import Containers
    from awm.config import edges
    from awm.trustpolicy.requests import file_request

    s = settings()
    shared = await Chrome(s["cdp"]).connect()
    boxes = Containers(image=s["seat_image"], seats_dir=s["dir"] / "seats",
                       tony_profile=Path(s["tony_profile"]) if s["tony_profile"] else None)
    book = SeatBook(shared, launch=boxes.launch, stop=boxes.stop)

    async def on_start() -> None:
        for site, url in s["service_sites"].items():
            await book.service_tab(site, url)

    hs = handlers(book, node=edges.here(), viewer_url=s["viewer"], file_request=file_request)
    await gatewayclient.ServiceAdapter(TOOL, API_MANIFEST, hs, on_start=on_start).run()


if __name__ == "__main__":
    asyncio.run(main())
