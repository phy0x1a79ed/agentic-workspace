"""The edge streams a sized request body upstream and reads anything else first."""

from __future__ import annotations

from starlette.requests import Request

from awm.httpsfront.proxy import _body


def _req(headers: dict[str, str], body: bytes = b"") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/invoke",
                    "headers": raw, "query_string": b""}, receive)


async def test_bodyless_request_is_empty_bytes():
    assert await _body(_req({})) == b""
    assert await _body(_req({"Content-Length": "0"})) == b""


async def test_sized_body_streams():
    assert not isinstance(await _body(_req({"Content-Length": "300000000"})), bytes)


async def test_chunked_body_is_read_and_sent_by_length():
    assert await _body(_req({"Transfer-Encoding": "chunked"}, b"abc")) == b"abc"
