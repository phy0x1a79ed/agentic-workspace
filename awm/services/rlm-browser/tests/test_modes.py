"""Unit tests for the canonical mode axis + the borrow-safe `attached` mode.

These are pure/in-process: no Chrome is launched and no external CDP endpoint is
dialed. The live end-to-end (attached drives the real desktop Chrome and googles
without a CAPTCHA) is exercised by the standalone driver, not here, so the suite
stays green on hosts with no desktop browser.
"""

from __future__ import annotations

import pytest

from awm.rlm_browser.browser import BrowserManager, _Session, _norm_mode
from awm.rlm_browser.hub_adapter import _mode_from_args

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


async def _noop_emit(topic, payload):  # pragma: no cover - trivial
    return None


# --- mode canonicalization ------------------------------------------------

@pytest.mark.parametrize("raw,want", [
    ("simple", "headless"),     # legacy alias
    ("Simple", "headless"),     # case-insensitive
    ("", "headless"),           # empty → default
    (None, "headless"),         # missing → default
    ("bogus", "headless"),      # unknown → default
    ("headless", "headless"),
    ("rendered", "rendered"),
    (" Attached ", "attached"), # trims + lowercases
])
def test_norm_mode(raw, want):
    assert _norm_mode(raw) == want


@pytest.mark.parametrize("args,want", [
    ({"graphics": True}, "rendered"),       # deprecated alias
    ({"graphics": False}, "headless"),
    ({"mode": "attached"}, "attached"),
    ({"mode": "attached", "graphics": True}, "attached"),  # explicit mode wins
    ({}, None),                              # neither → manager default
])
def test_mode_from_args(args, want):
    assert _mode_from_args(args) == want


# --- backend selection ----------------------------------------------------

def test_backend_for_routes_attached_separately():
    mgr = BrowserManager(_noop_emit)
    assert mgr._backend_for("attached").name == "attached"
    # headless/rendered share the env-selected launch backend (host by default).
    assert mgr._backend_for("headless").name == mgr._launch_backend.name
    assert mgr._backend_for("rendered").name == mgr._launch_backend.name


# --- singleton guard (one attached session / one window) ------------------

async def test_attached_singleton_rejects_second(awm_workspace):
    from awm.rlm_browser import dao
    dao.init()
    # Seed an active attached session directly in the isolated DB.
    dao.BrowserDAO().create_session(
        "game-a", mode="attached", backend="attached", cdp_port=19222)

    mgr = BrowserManager(_noop_emit)
    with pytest.raises(ValueError, match="attached session is already active"):
        await mgr.acquire("game-b", mode="attached")


# --- borrow-safe teardown -------------------------------------------------

class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.closed = False

    async def call(self, method, params=None, *, timeout=30.0):
        self.calls.append(method)
        return {}

    async def close(self) -> None:
        self.closed = True


class _FakeHandle:
    def __init__(self) -> None:
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


async def test_teardown_borrowed_never_closes_the_browser():
    """An attached (borrowed) session must NOT send Browser.close — that would
    kill the user's real Chrome — but our own sockets are still dropped."""
    mgr = BrowserManager(_noop_emit)
    sess = _Session("s1", cdp_base="http://172.25.176.1:19222",
                    mode="attached", handle=_FakeHandle(), borrowed=True)
    browser_conn, tab_conn = _FakeConn(), _FakeConn()
    sess._browser_conn = browser_conn
    sess._tab_conns = {"t": tab_conn}

    await mgr._teardown(sess)

    assert "Browser.close" not in browser_conn.calls   # never closed it
    assert browser_conn.closed and tab_conn.closed      # but we disconnected


async def test_teardown_owned_closes_the_browser():
    """A launched (owned) session is torn down via Browser.close as before."""
    mgr = BrowserManager(_noop_emit)
    sess = _Session("s2", cdp_base="http://127.0.0.1:9222",
                    mode="headless", handle=_FakeHandle(), borrowed=False)
    browser_conn = _FakeConn()
    sess._browser_conn = browser_conn

    await mgr._teardown(sess)

    assert "Browser.close" in browser_conn.calls
