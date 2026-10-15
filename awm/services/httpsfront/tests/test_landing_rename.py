"""Unit tests for the landing page's cosmetic rename feature: the
``page_names`` DAO methods on ``store.LandingDAO`` and the gated
``POST /__landing/name`` endpoint on the front."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from awm.httpsfront import proxy, store
from awm.httpsfront.auth import COOKIE_NAME

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

TOKEN = "signed.session.token"


class _StubGate:
    """Accepts exactly one session token; never refreshes."""

    async def authenticate(self, *, cookie=None, bearer=None):
        return (cookie == TOKEN), None

    async def session_ttl_seconds(self) -> float:
        return 3600.0

    async def verify_password(self, password: str):
        return None


def _client(app, *, authed: bool = True) -> TestClient:
    c = TestClient(app, base_url="https://front.example:12201",
                   follow_redirects=False)
    if authed:
        c.cookies.set(COOKIE_NAME, TOKEN, domain="front.example")
    return c


def _app():
    app = proxy.build_app("http://127.0.0.1:12203/", "/nonexistent/ca.pem",
                          landing=True)
    app.state.gate = _StubGate()
    return app


# --------------------------------------------------------------------------- #
# store.LandingDAO
# --------------------------------------------------------------------------- #

class TestLandingDAODisplayNames:
    def test_no_override_by_default(self, awm_workspace):
        store.init()
        dao = store.LandingDAO()
        assert dao.display_name("science") is None
        assert dao.display_names(["science"]) == {}

    def test_set_and_read_back(self, awm_workspace):
        store.init()
        dao = store.LandingDAO()
        dao.set_display_name("science", "Science Lab")
        assert dao.display_name("science") == "Science Lab"
        assert dao.display_names(["science", "other"]) == {"science": "Science Lab"}

    def test_set_overwrites_existing(self, awm_workspace):
        store.init()
        dao = store.LandingDAO()
        dao.set_display_name("science", "First")
        dao.set_display_name("science", "Second")
        assert dao.display_name("science") == "Second"

    def test_clear_removes_override(self, awm_workspace):
        store.init()
        dao = store.LandingDAO()
        dao.set_display_name("science", "Science Lab")
        dao.clear_display_name("science")
        assert dao.display_name("science") is None

    def test_set_blank_name_is_a_noop(self, awm_workspace):
        store.init()
        dao = store.LandingDAO()
        dao.set_display_name("science", "   ")
        assert dao.display_name("science") is None

    def test_display_names_bulk_empty_pages(self, awm_workspace):
        store.init()
        dao = store.LandingDAO()
        assert dao.display_names([]) == {}


# --------------------------------------------------------------------------- #
# POST /__landing/name
# --------------------------------------------------------------------------- #

class TestLandingNameEndpoint:
    def test_set_name_persists(self, awm_workspace):
        store.init()
        with _client(_app()) as c:
            r = c.post("/__landing/name", json={"page": "science", "name": "Science Lab"})
        assert r.status_code == 200
        assert r.json() == {"display_name": "Science Lab"}
        assert store.LandingDAO().display_name("science") == "Science Lab"

    def test_blank_name_clears_override(self, awm_workspace):
        store.init()
        store.LandingDAO().set_display_name("science", "Science Lab")
        with _client(_app()) as c:
            r = c.post("/__landing/name", json={"page": "science", "name": "   "})
        assert r.status_code == 200
        assert r.json() == {"display_name": None}
        assert store.LandingDAO().display_name("science") is None

    def test_missing_page_is_rejected(self, awm_workspace):
        store.init()
        with _client(_app()) as c:
            r = c.post("/__landing/name", json={"name": "Science Lab"})
        assert r.status_code == 400

    def test_anonymous_caller_is_denied(self, awm_workspace):
        store.init()
        with _client(_app(), authed=False) as c:
            r = c.post("/__landing/name", json={"page": "science", "name": "x"},
                       headers={"accept": "application/json"})
        assert r.status_code == 401

    def test_rename_does_not_touch_tags(self, awm_workspace):
        store.init()
        dao = store.LandingDAO()
        dao.add_tag("science", "physics")
        with _client(_app()) as c:
            r = c.post("/__landing/name", json={"page": "science", "name": "Science Lab"})
        assert r.status_code == 200
        assert dao.tags_for_page("science") == ["physics"]


# --------------------------------------------------------------------------- #
# GET / renders the override, not the technical name
# --------------------------------------------------------------------------- #

def test_root_renders_display_name_label_but_keeps_technical_href_and_key(awm_workspace):
    import httpx

    store.init()
    store.LandingDAO().set_display_name("science", "Science Lab")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "services": [{"kind": "page", "prefix": "/ui/science/", "name": "science"}]
        })

    app = _app()
    with _client(app) as c:
        c.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        r = c.get("/")

    assert r.status_code == 200
    body = r.text
    assert 'data-page="science"' in body
    assert 'href="/ui/science/"' in body
    assert ">Science Lab<" in body
