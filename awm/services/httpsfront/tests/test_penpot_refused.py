"""Penpot's own credential surface, refused at the edge.

awm holds a Penpot credential per person, signs them in with it and replaces it
nightly. That makes Penpot's own password commands not a second way in but a way
to break the first one, so the edge stops forwarding them.

What these pin: every refused command 404s through the door — through the
*classifier* and through a real request, not by asserting list membership, which
is the trap the prefix-stem fix already found once; both of upstream's RPC
prefixes are closed, since they route into the same method map; a
percent-encoded or differently-cased spelling does not walk past; and the
commands the app actually needs are untouched.
"""

from __future__ import annotations

from contextlib import contextmanager

import httpx
import pytest
from starlette.testclient import TestClient

from awm.httpsfront import penpot, policy, proxy
from awm.httpsfront.auth import COOKIE_NAME

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

TOKEN = "signed.session.token"
GATEWAY = "http://127.0.0.1:7819"
PENPOT = "http://127.0.0.1:9001"

REFUSED = sorted(penpot.NOT_FORWARDED)
ALLOWED = ["get-profile", "get-teams", "get-file", "update-profile",
           "logout", "push-audit-events"]


class _Gate:
    async def authenticate(self, *, cookie=None, bearer=None):
        return (True, None, "tony") if cookie == TOKEN else (False, None, None)

    async def session_ttl_seconds(self):
        return 3600.0

    async def verify_password(self, password):
        return None

    async def penpot_session(self, username, *, stale_token=None, refresh=False):
        return "penpot-tok-1"


async def _body():
    yield b"ok"


def _app(profile="public"):
    app = proxy.build_app(GATEWAY + "/", "/dev/null", profile=profile,
                          penpot_upstream=PENPOT)
    app.state.gate = _Gate()
    return app


@contextmanager
def _client(app):
    reached: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(str(request.url))
        return httpx.Response(200, content=_body())

    c = TestClient(app, base_url="https://nexus.example", follow_redirects=False)
    c.cookies.set(COOKIE_NAME, TOKEN, domain="nexus.example")
    with c:
        c.app.state.client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler))
        yield c, reached


# -- the classifier -----------------------------------------------------------


@pytest.mark.parametrize("command", REFUSED)
@pytest.mark.parametrize("prefix", penpot.RPC_PREFIXES)
def test_a_refused_command_is_denied_not_merely_unowned(command, prefix):
    """DENY, not OPEN. "/penpot/" is in ``OPEN_PREFIXES``, so a refusal that
    only made ``owns`` false would classify the command OPEN and forward it to
    the *gateway* — reachable, and pointed at the wrong service."""
    path = penpot.SHELL + prefix.lstrip("/") + command
    assert penpot.refused(path)
    assert not penpot.owns(path)
    assert policy.classify(path) is policy.Verdict.DENY


@pytest.mark.parametrize("command", ALLOWED)
@pytest.mark.parametrize("prefix", penpot.RPC_PREFIXES)
def test_the_commands_the_app_needs_are_untouched(command, prefix):
    path = penpot.SHELL + prefix.lstrip("/") + command
    assert not penpot.refused(path)
    assert policy.classify(path) is policy.Verdict.PENPOT


def test_both_of_upstreams_rpc_prefixes_are_closed():
    """Upstream routes /api/rpc/command/:m and /api/main/methods/:m into the
    same method map. A list that closed only the first would leave every
    refused command reachable under the second."""
    assert set(penpot.RPC_PREFIXES) == {"/api/rpc/command/",
                                        "/api/main/methods/"}
    for prefix in penpot.RPC_PREFIXES:
        assert penpot.refused(
            penpot.SHELL + prefix.lstrip("/") + "login-with-password")


def test_a_command_at_the_site_root_is_not_penpots_to_refuse():
    """The mount is what makes these Penpot's at all; outside it they are
    awm's own namespace and answer for their own reasons."""
    assert not penpot.refused("/api/rpc/command/login-with-password")


@pytest.mark.parametrize("path", [
    "/penpot/api/rpc/command/LOGIN-WITH-PASSWORD",
    "/penpot/api/rpc/command/Login-With-Password",
])
def test_a_different_capitalisation_does_not_walk_past(path):
    assert penpot.refused(path)
    assert policy.classify(path) is policy.Verdict.DENY


def test_a_query_string_is_not_part_of_the_command_name():
    assert penpot.refused("/penpot/api/rpc/command/login-with-password")


# -- through the door ---------------------------------------------------------


@pytest.mark.parametrize("command", REFUSED)
def test_a_refused_command_404s_and_never_reaches_an_upstream(command):
    with _client(_app()) as (c, reached):
        resp = c.post(penpot.SHELL + "api/rpc/command/" + command, json={})
    assert resp.status_code == 404
    assert reached == []


def test_a_percent_encoded_spelling_404s_too():
    """The edge routes on the decoded path; a refusal that only matched the
    literal bytes would be walked past by ``login-with-%70assword``."""
    with _client(_app()) as (c, reached):
        resp = c.post(penpot.SHELL + "api/rpc/command/login-with-%70assword",
                      json={})
    assert resp.status_code == 404
    assert reached == []


def test_the_same_refusal_holds_on_a_mesh_edge():
    """A mesh node's edge runs no profile and consults no policy at all, so the
    public door is not the enforcement there — the mount is."""
    with _client(_app(profile=None)) as (c, reached):
        resp = c.post(penpot.SHELL + "api/rpc/command/login-with-password",
                      json={})
    assert resp.status_code == 404
    assert reached == []


def test_the_commands_the_app_needs_still_reach_penpot():
    with _client(_app()) as (c, reached):
        assert c.post(penpot.SHELL + "api/rpc/command/get-profile",
                      json={}).status_code == 200
    assert reached == [PENPOT + "/api/rpc/command/get-profile"]


# -- the other half of the same refusal ---------------------------------------

COMPOSE = (__import__("pathlib").Path(__file__).resolve().parents[4]
           / "scripts" / "sirius" / "etc" / "penpot"
           / "docker-compose.sirius.yml")


def _sirius_flags() -> set[str]:
    for line in COMPOSE.read_text().splitlines():
        if line.strip().startswith("PENPOT_FLAGS:"):
            return set(line.split(":", 1)[1].strip().strip('"').split())
    raise AssertionError(f"no PENPOT_FLAGS in {COMPOSE}")


def test_the_public_stack_also_refuses_registration_at_the_backend():
    """The edge list and this flag are two independent closures of one hole. A
    reader who removes either should have to remove the other on purpose."""
    assert "disable-registration" in _sirius_flags()


def test_the_public_stack_does_not_disable_password_login():
    """It would be the obvious flag to reach for and it is the wrong one: the
    exporter authenticates by cookie only and takes no access token, so turning
    password login off inside Penpot blanks every diagram."""
    assert "disable-login-with-password" not in _sirius_flags()


@pytest.mark.parametrize("flag", ["enable-demo-users",
                                  "disable-secure-session-cookies"])
def test_the_local_only_flags_never_reach_the_public_stack(flag):
    assert flag not in _sirius_flags()
