"""Browser seats (fleet.md R102): each attached session drives its own window.

A seat opens, switches and closes the tabs of its own window and nothing else.
No seat reaches the raw debug port: the ``cdp`` verb runs a page-level method on
a tab the seat owns, from an allowlist. The trust level decides the profile
(item 6): a trusted seat may use Tony's profile, a cooperative seat always gets
a blank one, and an untrusted session gets no seat.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from awm.browser_seats.cdp import Cdp

LEVELS = ("untrusted", "cooperative", "trusted")
PROFILES = ("tony", "blank")

# Page-level domains a seat may drive on its own tabs.
SEAT_DOMAINS = {"Page", "Runtime", "Input", "DOM", "DOMSnapshot", "Accessibility", "Emulation",
                "Log", "CSS", "Overlay", "Network"}
# Methods inside those domains that reach past the tab: other sites' cookies and the like.
SEAT_DENIED = {"Network.getAllCookies", "Network.getCookies", "Network.setCookie", "Network.setCookies",
               "Network.deleteCookies", "Network.clearBrowserCookies", "Network.clearBrowserCache",
               "Network.loadNetworkResource", "Page.close", "Page.navigateToHistoryEntry"}


class SeatError(PermissionError):
    pass


@dataclass
class Seat:
    seat_id: str
    session: str
    level: str
    profile: str
    context: str | None             # a blank browser context; None is Tony's default profile
    cdp: Cdp                        # the shared Chrome, or the seat's own container
    container: str | None = None
    window: int | None = None
    tabs: dict[str, str] = field(default_factory=dict)   # targetId -> flat sessionId

    def view(self) -> dict[str, Any]:
        return {"seat": self.seat_id, "level": self.level, "profile": self.profile,
                "container": self.container, "window": self.window, "tabs": sorted(self.tabs)}


Launcher = Callable[[str, str], Awaitable[tuple[str, Cdp]]]   # (seat_id, profile) -> (container, cdp)


class SeatBook:
    """Every seat on the shared Chrome and in the seats' own containers."""

    def __init__(self, shared: Cdp, *, launch: Launcher | None = None,
                 stop: Callable[[str], Awaitable[None]] | None = None) -> None:
        self.shared = shared
        self.launch = launch
        self.stop = stop
        self.seats: dict[str, Seat] = {}
        self._lock = asyncio.Lock()
        self.service_window: int | None = None
        self.service_tabs: dict[str, str] = {}   # site -> targetId

    # -- opening and closing ------------------------------------------------

    async def open(self, session: str, level: str, *, profile: str | None = None,
                   own_container: bool = False) -> Seat:
        if level not in LEVELS:
            raise ValueError(f"unknown trust level {level!r}")
        if level == "untrusted":
            raise SeatError("an untrusted session gets no browser seat")
        profile = profile or ("tony" if level == "trusted" else "blank")
        if profile not in PROFILES:
            raise ValueError(f"profile is one of {PROFILES}")
        if profile == "tony" and level != "trusted":
            raise SeatError("only a trusted session gets Tony's profile (fleet.md R102 item 6)")
        if not session:
            raise SeatError("a seat belongs to a session: no calling session reached the browser")
        seat_id = secrets.token_urlsafe(12)
        if own_container:
            if self.launch is None:
                raise SeatError("this browser runs no per-seat containers")
            container, cdp = await self.launch(seat_id, profile)
            seat = Seat(seat_id, session, level, profile, None, cdp, container=container)
        elif profile == "tony":
            seat = Seat(seat_id, session, level, profile, None, self.shared)
        else:
            ctx = (await self.shared.send("Target.createBrowserContext", {"disposeOnDetach": False}))["browserContextId"]
            seat = Seat(seat_id, session, level, profile, ctx, self.shared)
        self.seats[seat_id] = seat
        await self.tab_open(seat_id, session, "about:blank", new_window=True)
        return seat

    async def close(self, seat_id: str, session: str) -> None:
        seat = self._seat(seat_id, session)
        for target in list(seat.tabs):
            await seat.cdp.send("Target.closeTarget", {"targetId": target})
        if seat.context:
            await seat.cdp.send("Target.disposeBrowserContext", {"browserContextId": seat.context})
        if seat.container and self.stop:
            await self.stop(seat.container)
        del self.seats[seat_id]

    def _seat(self, seat_id: str, session: str) -> Seat:
        seat = self.seats.get(seat_id)
        if seat is None or seat.session != session:
            raise SeatError("no such seat for this session")
        return seat

    def _tab(self, seat: Seat, target: str) -> str:
        if target not in seat.tabs:
            raise SeatError("that tab is not in this seat's window")
        return seat.tabs[target]

    # -- tabs ---------------------------------------------------------------

    async def tab_open(self, seat_id: str, session: str, url: str, *, new_window: bool = False) -> str:
        seat = self._seat(seat_id, session)
        async with self._lock:
            params: dict[str, Any] = {"url": url}
            if seat.context:
                params["browserContextId"] = seat.context
            if new_window or not seat.tabs:
                params["newWindow"] = True
            else:
                # A new tab lands in the focused window, so focus this seat's first.
                await seat.cdp.send("Target.activateTarget", {"targetId": next(iter(seat.tabs))})
            target = (await seat.cdp.send("Target.createTarget", params))["targetId"]
            window = (await seat.cdp.send("Browser.getWindowForTarget", {"targetId": target}))["windowId"]
            if seat.window is not None and window != seat.window:
                await seat.cdp.send("Target.closeTarget", {"targetId": target})
                raise SeatError("the tab opened outside this seat's window; try again")
            seat.window = window
            flat = (await seat.cdp.send("Target.attachToTarget", {"targetId": target, "flatten": True}))["sessionId"]
            seat.tabs[target] = flat
        return target

    async def tab_close(self, seat_id: str, session: str, target: str) -> None:
        seat = self._seat(seat_id, session)
        self._tab(seat, target)
        await seat.cdp.send("Target.closeTarget", {"targetId": target})
        del seat.tabs[target]

    async def tab_activate(self, seat_id: str, session: str, target: str) -> None:
        seat = self._seat(seat_id, session)
        self._tab(seat, target)
        await seat.cdp.send("Target.activateTarget", {"targetId": target})

    async def tab_list(self, seat_id: str, session: str) -> list[dict[str, Any]]:
        seat = self._seat(seat_id, session)
        infos = (await seat.cdp.send("Target.getTargets", {}))["targetInfos"]
        return [{"tab": t["targetId"], "url": t.get("url"), "title": t.get("title")}
                for t in infos if t["targetId"] in seat.tabs]

    async def cdp(self, seat_id: str, session: str, target: str, method: str,
                  params: dict | None = None) -> dict:
        seat = self._seat(seat_id, session)
        flat = self._tab(seat, target)
        if method.split(".", 1)[0] not in SEAT_DOMAINS or method in SEAT_DENIED:
            raise SeatError(f"{method} is not open to a seat")
        return await seat.cdp.send(method, params or {}, session=flat)

    async def navigate(self, seat_id: str, session: str, target: str, url: str) -> dict:
        return await self.cdp(seat_id, session, target, "Page.navigate", {"url": url})

    # -- the viewer ---------------------------------------------------------

    async def raise_window(self, seat_id: str, session: str | None = None) -> None:
        """Bring a seat's window to the front of the one viewer (item 3)."""
        seat = self.seats.get(seat_id)
        if seat is None or (session is not None and seat.session != session):
            raise SeatError("no such seat for this session")
        if seat.window is not None:
            await seat.cdp.send("Browser.setWindowBounds", {"windowId": seat.window, "bounds": {"windowState": "normal"}})
        if seat.tabs:
            await seat.cdp.send("Target.activateTarget", {"targetId": next(iter(seat.tabs))})

    # -- the service window (item 4) ----------------------------------------

    async def service_tab(self, site: str, url: str) -> str:
        """One resident tab per site in the service window, driven only by the singleton."""
        if site in self.service_tabs:
            return self.service_tabs[site]
        async with self._lock:
            params: dict[str, Any] = {"url": url, "newWindow": self.service_window is None}
            if self.service_window is not None and self.service_tabs:
                await self.shared.send("Target.activateTarget", {"targetId": next(iter(self.service_tabs.values()))})
            target = (await self.shared.send("Target.createTarget", params))["targetId"]
            self.service_window = (await self.shared.send("Browser.getWindowForTarget", {"targetId": target}))["windowId"]
            self.service_tabs[site] = target
        return target

    def list(self, session: str | None = None) -> list[dict[str, Any]]:
        return [s.view() for s in self.seats.values() if session is None or s.session == session]
