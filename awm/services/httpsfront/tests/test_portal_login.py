"""Sign-in with alcor's passkey grant (fleet.md R84, users.md R130).

What these pin: the callback takes only a ``portal_login`` grant signed by the
configured key, for this node, this host and this path, and only once; success
is a 302 to the local path with both cookies set; anything else sets nothing.
"""

from __future__ import annotations

import base64
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from starlette.testclient import TestClient
from verify_grant import canon, key_id, payload_digest

from awm.httpsfront import proxy
from awm.tlsfront import portal
from awm.tlsfront.auth import AS_COOKIE_NAME, COOKIE_NAME

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

HOST = "front.example"
KEY = Ed25519PrivateKey.generate()


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def grant(*, node="altair", host=HOST, path="/trilium/", kind="portal_login", key=KEY,
          ttl=60, grant_id="gabc") -> str:
    now = int(time.time())
    body = {"grant_id": grant_id, "request_id": "", "kind": kind, "node": node,
            "subject": "tony", "payload_sha256": payload_digest({"site": host, "return": path}),
            "not_before": now, "not_after": now + ttl, "approved_by_device": "d1"}
    env = {"grant": body, "sig": _b64u(key.sign(canon(body))), "key_id": key_id(key.public_key())}
    return portal.encode_grant(env)


class _Gate:
    async def authenticate(self, *, cookie=None, bearer=None, shape=None):
        return (True, None, "tony") if cookie == "minted-tony" else (False, None, None)

    async def mint_session(self, sub):
        return f"minted-{sub}", 3600

    async def session_ttl_seconds(self):
        return 3600.0


@pytest.fixture
def client(monkeypatch):
    pub = KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    monkeypatch.setenv(portal.GRANT_KEY_ENV, _b64u(pub))
    monkeypatch.setenv("AWM_NODE_NAME", "altair")
    monkeypatch.setenv(portal.ORIGIN_ENV, "https://auth.example")
    app = proxy.build_app("http://127.0.0.1:1/", "/nonexistent/ca.pem", profile="public")
    app.state.gate = _Gate()
    return TestClient(app, base_url=f"https://{HOST}", follow_redirects=False)


def back(c, g, path="/trilium/"):
    return c.get(f"{portal.CALLBACK}?grant={g}&next={path}")


def test_a_good_grant_sets_the_session_and_goes_to_next(client):
    r = back(client, grant())
    assert r.status_code == 302 and r.headers["location"] == "/trilium/"
    assert r.cookies[COOKIE_NAME] == "minted-tony" and r.cookies[AS_COOKIE_NAME] == "tony"
    setc = "\n".join(r.headers.get_list("set-cookie")).lower()
    assert "httponly" in setc and setc.count("samesite=strict") == 2
    assert r.headers["cache-control"] == "no-store"


def test_a_grant_is_spent_once(client):
    g = grant(grant_id="gonce")
    assert back(client, g).status_code == 302
    assert back(client, g).status_code == 403


@pytest.mark.parametrize("bad", [
    {"node": "capella"}, {"host": "other.example"}, {"path": "/elsewhere"},
    {"kind": "admin_ssh"}, {"key": Ed25519PrivateKey.generate()}, {"ttl": -1},
])
def test_a_grant_for_anything_else_is_refused(client, bad):
    r = back(client, grant(**bad))
    assert r.status_code == 403 and not r.cookies


def test_next_must_be_a_local_path(client):
    for path in ("https://evil.example/", "//evil.example/", "/a?b=c"):
        assert back(client, grant(path=path), path=path).status_code == 400


def test_no_key_refuses_every_grant(client, monkeypatch):
    monkeypatch.delenv(portal.GRANT_KEY_ENV)
    assert back(client, grant()).status_code == 403


def test_a_browser_with_no_session_goes_to_the_approver(client):
    r = client.get("/trilium/", headers={"Accept": "text/html"})
    assert r.status_code == 302
    assert r.headers["location"] == ("https://auth.example/portal_login?next="
                                     "https%3A%2F%2Ffront.example%2Ftrilium%2F")


def test_the_password_forms_are_gone(client):
    assert client.post("/__auth/login", json={"password": "x"}).status_code != 200
    assert client.get("/__auth/link?p=x").status_code != 302
