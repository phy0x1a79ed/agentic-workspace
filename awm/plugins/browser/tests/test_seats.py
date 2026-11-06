"""Browser seats against a fake Chrome: confinement, trust and the viewer (fleet.md R102)."""

from __future__ import annotations

import itertools

import pytest

from awm.browser_seats import hub_adapter
from awm.browser_seats.seats import SeatBook, SeatError

pytestmark = [pytest.mark.unit]


class FakeChrome:
    """Windows hold targets. A new tab lands in the focused window, as Chrome does."""

    def __init__(self, name: str = "shared") -> None:
        self.name = name
        self.ids = itertools.count(1)
        self.windows: dict[str, int] = {}
        self.contexts: set[str] = set()
        self.focused: int | None = None
        self.calls: list[tuple[str, dict, str | None]] = []

    async def send(self, method, params=None, session=None):
        params = params or {}
        self.calls.append((method, params, session))
        if method == "Target.createBrowserContext":
            ctx = f"ctx{next(self.ids)}"
            self.contexts.add(ctx)
            return {"browserContextId": ctx}
        if method == "Target.createTarget":
            t = f"{self.name}-t{next(self.ids)}"
            if params.get("newWindow") or self.focused is None:
                self.focused = 1000 + next(self.ids)
            self.windows[t] = self.focused
            return {"targetId": t}
        if method == "Target.activateTarget":
            self.focused = self.windows[params["targetId"]]
            return {}
        if method == "Browser.getWindowForTarget":
            return {"windowId": self.windows[params["targetId"]]}
        if method == "Target.attachToTarget":
            return {"sessionId": "s-" + params["targetId"]}
        if method == "Target.closeTarget":
            self.windows.pop(params["targetId"], None)
            return {"success": True}
        if method == "Target.getTargets":
            return {"targetInfos": [{"targetId": t, "url": "about:blank", "title": ""} for t in self.windows]}
        return {"ok": method}


@pytest.fixture()
def chrome():
    return FakeChrome()


async def test_a_trusted_seat_gets_tonys_profile_and_a_cooperative_one_a_blank_context(chrome):
    book = SeatBook(chrome)
    t = await book.open("job-t", "trusted")
    c = await book.open("job-c", "cooperative")
    assert t.profile == "tony" and t.context is None
    assert c.profile == "blank" and c.context in chrome.contexts
    assert t.window != c.window


@pytest.mark.parametrize("level, profile", [("cooperative", "tony")])
async def test_only_trusted_gets_tonys_profile(chrome, level, profile):
    with pytest.raises(SeatError, match="trusted"):
        await SeatBook(chrome).open("job", level, profile=profile)


async def test_untrusted_gets_no_seat(chrome):
    with pytest.raises(SeatError, match="untrusted"):
        await SeatBook(chrome).open("job", "untrusted")


async def test_a_seat_reaches_only_its_own_tabs(chrome):
    book = SeatBook(chrome)
    a = await book.open("job-a", "trusted")
    b = await book.open("job-b", "trusted")
    tb = next(iter(b.tabs))
    with pytest.raises(SeatError):
        await book.tab_close(a.seat_id, "job-a", tb)
    with pytest.raises(SeatError):
        await book.cdp(a.seat_id, "job-a", tb, "Runtime.evaluate", {"expression": "1"})
    with pytest.raises(SeatError):
        await book.tab_list(b.seat_id, "job-a")


async def test_new_tabs_stay_in_the_seats_window(chrome):
    book = SeatBook(chrome)
    a = await book.open("job-a", "trusted")
    b = await book.open("job-b", "trusted")
    # b's window has focus now; a's next tab must still land in a's window.
    t = await book.tab_open(a.seat_id, "job-a", "https://example.org")
    assert chrome.windows[t] == a.window
    assert {chrome.windows[x] for x in a.tabs} == {a.window}
    assert {chrome.windows[x] for x in b.tabs} == {b.window}


@pytest.mark.parametrize("method", ["Target.createTarget", "Browser.close", "Storage.getCookies",
                                    "Network.getAllCookies", "SystemInfo.getInfo"])
async def test_no_seat_reaches_the_raw_debug_port(chrome, method):
    book = SeatBook(chrome)
    a = await book.open("job-a", "trusted")
    with pytest.raises(SeatError, match="not open"):
        await book.cdp(a.seat_id, "job-a", next(iter(a.tabs)), method)


async def test_page_methods_run_on_the_tabs_own_session(chrome):
    book = SeatBook(chrome)
    a = await book.open("job-a", "cooperative")
    tab = next(iter(a.tabs))
    await book.navigate(a.seat_id, "job-a", tab, "https://example.org")
    assert chrome.calls[-1] == ("Page.navigate", {"url": "https://example.org"}, "s-" + tab)


async def test_close_disposes_the_blank_context(chrome):
    book = SeatBook(chrome)
    c = await book.open("job-c", "cooperative")
    await book.close(c.seat_id, "job-c")
    assert ("Target.disposeBrowserContext", {"browserContextId": c.context}, None) in chrome.calls
    assert book.list() == []


async def test_own_container_carries_the_profile_the_level_allows(chrome):
    launched = []

    async def launch(seat_id, profile):
        launched.append(profile)
        return f"box-{seat_id}", FakeChrome("box")

    book = SeatBook(chrome, launch=launch)
    await book.open("job-t", "trusted", own_container=True)
    await book.open("job-t2", "trusted", profile="blank", own_container=True)
    await book.open("job-c", "cooperative", own_container=True)
    assert launched == ["tony", "blank", "blank"]


async def test_service_window_holds_one_tab_per_site(chrome):
    book = SeatBook(chrome)
    teams = await book.service_tab("teams", "https://teams.microsoft.com")
    slack = await book.service_tab("slack", "https://app.slack.com")
    assert await book.service_tab("teams", "https://teams.microsoft.com") == teams
    assert chrome.windows[teams] == chrome.windows[slack] == book.service_window
    a = await book.open("job-a", "trusted")
    with pytest.raises(SeatError):
        await book.tab_close(a.seat_id, "job-a", teams)


async def test_signin_files_a_request_and_raises_the_window(chrome):
    book = SeatBook(chrome)
    filed = []
    hs = hub_adapter.handlers(book, node="alcor", viewer_url="https://viewer",
                              file_request=lambda kind, **kw: filed.append((kind, kw)) or "r1")
    who = {"_caller_session": {"job": "job-a"}, "_caller_trust": "trusted"}
    seat = await hs["seat_open"](dict(who))
    out = await hs["signin"]({**who, "seat": seat["seat"], "site": "github.com", "reason": "captcha"})
    assert out == {"request_id": "r1", "viewer": "https://viewer"}
    assert filed[0][0] == "browser_signin"
    assert filed[0][1]["payload"] == {"seat": seat["seat"], "site": "github.com"}
    assert chrome.calls[-1][0] == "Target.activateTarget"


async def test_a_call_with_no_level_is_untrusted(chrome):
    hs = hub_adapter.handlers(SeatBook(chrome), node="alcor", viewer_url="")
    unstamped = await hs["seat_open"]({"_caller_session": {"job": "j"}})
    untrusted = await hs["seat_open"]({"_caller_session": {"job": "j"}, "_caller_trust": "untrusted"})
    assert "error" in unstamped and unstamped == untrusted
    seat = await hs["seat_open"]({"_caller_session": {"job": "j"}, "_caller_trust": "cooperative"})
    assert seat["profile"] == "blank"
    refused = await hs["seat_open"]({"_caller_session": {"job": "j"}, "_caller_trust": "cooperative",
                                     "profile": "tony"})
    assert "trusted" in refused["error"]
