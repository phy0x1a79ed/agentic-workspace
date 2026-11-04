"""A service's call to another tool goes to ``/invoke`` and names the service by its token."""

import asyncio
import json

import httpx
import pytest

from awm import gatewayclient as gc

_REAL_ASYNC, _REAL_SYNC = httpx.AsyncClient, httpx.Client


@pytest.fixture()
def gateway(monkeypatch, tmp_path):
    seen = []
    replies = {"result": json.dumps({"ok": True})}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(request.headers), json.loads(request.content)))
        return httpx.Response(200, json=replies)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _REAL_ASYNC(transport=transport))
    monkeypatch.setattr(httpx, "Client", lambda **kw: _REAL_SYNC(transport=transport))
    monkeypatch.setenv("AWM_HUB_URL", "http://gateway.test/")
    token = tmp_path / "svc.token"
    token.write_text("t0k3n\n")
    monkeypatch.setenv("AWM_HUB_TOKEN_FILE", str(token))
    return seen, replies


def test_a_call_posts_the_flat_name_with_the_service_token(gateway):
    seen, _ = gateway
    assert asyncio.run(gc.call("scope", "resolve", {"project": "awm"}, caller_pid=42)) == {"ok": True}
    path, headers, body = seen[0]
    assert path == "/invoke"
    assert body == {"name": "scope_resolve", "args": {"project": "awm"}}
    assert headers["x-awm-service-token"] == "t0k3n"
    assert headers["x-awm-caller-pid"] == "42"
    assert gc.call_sync("harness-cc", "list") == {"ok": True}
    assert seen[1][2] == {"name": "harness-cc_list", "args": {}}


def test_a_result_is_decoded_and_null_reads_as_empty(gateway):
    _, replies = gateway
    replies["result"] = "plain words"
    assert gc.call_sync("journal", "render") == "plain words"
    replies["result"] = "null"
    assert gc.call_sync("journal", "render") == {}
    replies["result"] = "[1, 2]"
    assert gc.call_sync("journal", "render") == [1, 2]


def test_no_token_file_sends_no_token(gateway, monkeypatch):
    seen, _ = gateway
    monkeypatch.delenv("AWM_HUB_TOKEN_FILE")
    gc.call_sync("journal", "render")
    assert "x-awm-service-token" not in seen[0][1]


def test_a_refusal_raises_with_its_status(monkeypatch):
    transport = httpx.MockTransport(lambda r: httpx.Response(403, text="not declared"))
    monkeypatch.setattr(httpx, "Client", lambda **kw: _REAL_SYNC(transport=transport))
    with pytest.raises(gc.GatewayCallError) as exc:
        gc.call_sync("journal", "post")
    assert exc.value.status == 403 and "journal/post" in str(exc.value)
