"""The edge streams request bodies upstream, and frames a bodyless request as empty."""

from __future__ import annotations

from starlette.requests import Request

from awm.httpsfront.proxy import _body


def _req(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "POST", "path": "/invoke",
                    "headers": raw, "query_string": b""})


def test_bodyless_request_is_empty_bytes():
    assert _body(_req({})) == b""
    assert _body(_req({"Content-Length": "0"})) == b""


def test_sized_body_streams():
    assert not isinstance(_body(_req({"Content-Length": "300000000"})), bytes)


def test_chunked_body_streams():
    assert not isinstance(_body(_req({"Transfer-Encoding": "chunked"})), bytes)
