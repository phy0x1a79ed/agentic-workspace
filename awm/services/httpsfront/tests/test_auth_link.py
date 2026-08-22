"""``GET /__auth/link`` — the autologin the Discord password push carries.

A live credential in a URL is only acceptable if the URL is never a page. Every
test here pins one half of that: the success path 302s with the cookie set and
renders nothing, and the failure path sets nothing and echoes nothing.

The redirect-target validation is the other half — ``to=`` must not be a way to
bounce a freshly-authenticated browser off this origin.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from awm.httpsfront import proxy
from awm.httpsfront.auth import COOKIE_NAME

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

GOOD = "correct-horse"
TOKEN = "signed.session.token"


class _StubGate:
    """Stands in for AuthGate: accepts exactly one password, nothing else."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def verify_password(self, password: str) -> str | None:
        self.seen.append(password)
        return TOKEN if password == GOOD else None

    async def session_ttl_seconds(self) -> float:
        return 3600.0

    async def authenticate(self, *, cookie=None, bearer=None):
        return (cookie == TOKEN), None


@pytest.fixture()
def client():
    app = proxy.build_app("http://127.0.0.1:1/", "/nonexistent/ca.pem")
    app.state.gate = _StubGate()
    # https base URL, not http: the session cookie is Secure, so an http client
    # would silently drop it and every cookie assertion here would pass for the
    # wrong reason.
    with TestClient(app, base_url="https://testserver",
                    follow_redirects=False) as c:
        yield c


# ---------------------------------------------------------------------------
# The success path
# ---------------------------------------------------------------------------

def test_good_password_redirects_and_sets_the_cookie(client):
    r = client.get("/__auth/link", params={"p": GOOD})
    assert r.status_code == 302
    assert r.headers["location"] == "/"
    assert COOKIE_NAME in r.cookies
    assert r.cookies[COOKIE_NAME] == TOKEN


def test_the_response_body_is_empty(client):
    """Nothing is ever rendered under the address that carried the password."""
    r = client.get("/__auth/link", params={"p": GOOD})
    assert r.text == ""


def test_the_response_is_uncacheable_and_leaks_no_referrer(client):
    r = client.get("/__auth/link", params={"p": GOOD})
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_the_cookie_is_httponly_and_secure(client):
    r = client.get("/__auth/link", params={"p": GOOD})
    setc = r.headers["set-cookie"].lower()
    assert "httponly" in setc
    assert "secure" in setc
    assert "samesite=lax" in setc


def test_the_cookie_actually_authenticates_the_next_request(client):
    """End-to-end: the redirect's cookie is one the edge accepts."""
    client.get("/__auth/link", params={"p": GOOD})
    r = client.get("/__auth/whoami")
    assert r.status_code == 200
    assert r.json() == {"user": "operator"}


# ---------------------------------------------------------------------------
# The failure path
# ---------------------------------------------------------------------------

def test_bad_password_sets_no_cookie(client):
    r = client.get("/__auth/link", params={"p": "wrong"})
    assert r.status_code == 302
    assert "set-cookie" not in r.headers
    assert COOKIE_NAME not in r.cookies


def test_bad_password_never_echoes_the_submitted_value(client):
    r = client.get("/__auth/link", params={"p": "s3cret-guess"})
    assert "s3cret-guess" not in r.text
    assert "s3cret-guess" not in r.headers["location"]
    assert not any("s3cret-guess" in v for v in r.headers.values())


def test_bad_password_lands_on_the_login_form(client):
    r = client.get("/__auth/link", params={"p": "wrong"})
    assert r.headers["location"] == "/"
    landed = client.get("/", headers={"accept": "text/html"})
    assert landed.status_code == 200
    assert "password" in landed.text.lower()


def test_a_missing_password_is_just_a_failed_login(client):
    r = client.get("/__auth/link")
    assert r.status_code == 302
    assert "set-cookie" not in r.headers


def test_a_post_cannot_mint_a_session(client):
    """The route is GET-only. A POST is not a 405 — it falls through to the
    catch-all proxy, exactly like a GET to ``/__auth/login`` does — but the one
    thing that must not happen is it authenticating anybody."""
    r = client.post("/__auth/link", params={"p": GOOD})
    assert r.status_code == 401
    assert "set-cookie" not in r.headers


# ---------------------------------------------------------------------------
# The `to=` redirect target
# ---------------------------------------------------------------------------

def test_a_local_target_is_honoured(client):
    r = client.get("/__auth/link", params={"p": GOOD, "to": "/ui/notes/"})
    assert r.headers["location"] == "/ui/notes/"
    assert r.cookies[COOKIE_NAME] == TOKEN


@pytest.mark.parametrize("hostile", [
    "https://evil.example/",            # absolute URL
    "//evil.example/",                  # protocol-relative authority
    "/\\evil.example/",                 # browsers normalise \ to / here
    "ui/notes/",                        # relative — resolves against the referer
    "/ok\r\nSet-Cookie: x=1",           # header splitting
    "/ok\nLocation: https://evil",      # ditto
])
def test_a_non_local_target_falls_back_to_root(client, hostile):
    r = client.get("/__auth/link", params={"p": GOOD, "to": hostile})
    assert r.headers["location"] == "/"
    assert r.cookies[COOKIE_NAME] == TOKEN


def test_query_and_fragment_are_dropped_from_the_target(client):
    r = client.get("/__auth/link", params={"p": GOOD, "to": "/ui/notes/?id=5#top"})
    assert r.headers["location"] == "/ui/notes/"


def test_a_hostile_target_is_rejected_before_the_password_is_checked(client):
    """Target validation must not depend on the login succeeding."""
    r = client.get("/__auth/link", params={"p": "wrong", "to": "https://evil/"})
    assert r.headers["location"] == "/"


# ---------------------------------------------------------------------------
# safe_local_path in isolation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (None, "/"),
    ("", "/"),
    ("/", "/"),
    ("/ui/notes/", "/ui/notes/"),
    ("/ui/notes", "/ui/notes"),
    ("//evil", "/"),
    ("/\\evil", "/"),
    ("http://evil/", "/"),
    ("?", "/"),
    ("/?a=b", "/"),
])
def test_safe_local_path(raw, expected):
    assert proxy.safe_local_path(raw) == expected
