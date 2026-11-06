"""The edge must not write request query strings anywhere.

`GET /__auth/portal?grant=<grant>` puts a live credential in a URL. That is only
acceptable while nothing logs it — and the thing that actually suppresses it is
one uvicorn knob.

uvicorn emits access records on the `uvicorn.access` logger at INFO. Pinning
`log_level="warning"` is therefore a security control, and this test exists so
that someone raising it to chase an unrelated bug fails here first rather than
discovering grants in a log file later.
"""

from __future__ import annotations

import inspect

import pytest

from awm.tlsfront import proxy

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def test_serve_pins_uvicorn_below_access_log_level():
    src = inspect.getsource(proxy.serve_app)
    assert 'log_level="warning"' in src, (
        "the edge's uvicorn log level must stay at warning or higher — INFO "
        "turns on access logging, which records the full query string, and "
        "/__auth/portal carries a live sign-in grant in its query string"
    )


def test_the_reason_is_recorded_next_to_the_knob():
    """A bare setting gets 'cleaned up'. The why has to travel with it."""
    src = inspect.getsource(proxy.serve_app)
    assert "SECURITY" in src
    assert "__auth/portal" in src


def test_the_ws_failure_path_logs_no_query_string():
    src = inspect.getsource(proxy.bridge)
    # `up_url` carries the query; the failure log must use the bare path instead.
    assert 'log.debug("ws upstream connect failed for %s: %s", ws.url.path' in src
