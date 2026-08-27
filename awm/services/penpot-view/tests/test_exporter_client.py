"""The export -> fetch round trip, and the ways it goes silently wrong.

Every scenario pinned here is a way the client could return a blank or stale
SVG while looking like it succeeded: fetching the asset from the wrong host,
accepting an empty or mistyped body, or retrying an authentication failure
into an infinite loop instead of surfacing it. None of this touches a live
Penpot -- ``httpx.MockTransport`` stands in for both the frontend and the
exporter.
"""

from __future__ import annotations

import uuid as uuidlib

import json

import httpx
import pytest

from awm.penpot_view import exporter_client as EC

BASE_URL = "http://penpot.example"
EXPORTER_URL = "http://exporter.example"

FILE_ID = "0197f9d2-1a2b-73aa-8b9c-1234567890ab"
PAGE_ID = "0197f9d2-1a2b-73aa-8b9c-1234567890ac"
OBJECT_ID = "0197f9d2-1a2b-73aa-8b9c-1234567890ad"
ASSET_ID = "0197f9d2-1a2b-73aa-8b9c-1234567890ae"
PROFILE_ID = uuidlib.UUID("0197f9d2-1a2b-73aa-8b9c-1234567890af")

ASSET_URI = f"{BASE_URL}/assets/by-id/{ASSET_ID}"
SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>'


# --- fake Penpot -------------------------------------------------------

def _login_handler(*, token: str = "svc-token", profile_id=PROFILE_ID):
    def handler(request: httpx.Request) -> httpx.Response:
        body = EC._transit_dumps({EC.Keyword("id"): profile_id})
        return httpx.Response(
            200, text=body,
            headers={
                "content-type": "application/transit+json",
                "set-cookie": f"auth-token={token}; Path=/; HttpOnly",
            },
        )
    return handler


def _export_ok_handler(*, uri: str = ASSET_URI):
    def handler(request: httpx.Request) -> httpx.Response:
        body = EC._transit_dumps({EC.Keyword("uri"): uri})
        return httpx.Response(200, text=body,
                              headers={"content-type": "application/transit+json"})
    return handler


def _asset_handler(*, status: int = 200, body: bytes | None = SVG_BYTES,
                   content_type: str | None = "image/svg+xml",
                   extra_headers: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        headers = dict(extra_headers or {})
        if content_type is not None:
            headers["content-type"] = content_type
        return httpx.Response(status, content=(body or b""), headers=headers)
    return handler


def _router(*, login=None, export=None, asset=None):
    login = login or _login_handler()
    export = export or _export_ok_handler()
    asset = asset or _asset_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EC.LOGIN_PATH:
            return login(request)
        if request.url.path == EC.EXPORT_PATH:
            return export(request)
        return asset(request)
    return handler


def _client(*, login=None, export=None, asset=None) -> EC.ExporterClient:
    transport = httpx.MockTransport(_router(login=login, export=export, asset=asset))
    return EC.ExporterClient(base_url=BASE_URL, exporter_url=EXPORTER_URL,
                             username="svc-account", password="hunter2",
                             transport=transport)


def _export(client: EC.ExporterClient) -> bytes:
    return client.export_svg(file_id=FILE_ID, page_id=PAGE_ID,
                             object_id=OBJECT_ID, name="board")


# --- the happy path ----------------------------------------------------

def test_happy_path_export_returns_the_rendered_svg_bytes():
    assert _export(_client()) == SVG_BYTES


# --- the headline regression --------------------------------------------

def test_204_with_x_accel_redirect_is_a_hard_error_not_an_empty_success():
    """serve-object-from-fs answers a bare (non-nginx) asset request with
    HTTP 204 and zero bytes -- a client that reads that as "done" produces a
    blank SVG that looks exactly like a successful export."""
    asset = _asset_handler(status=204, body=b"", content_type=None,
                           extra_headers={"x-accel-redirect": "/internal/assets/x"})
    with pytest.raises(EC.ExporterError, match="204"):
        _export(_client(asset=asset))


# --- other ways an asset response lies ----------------------------------

def test_zero_byte_asset_body_is_a_hard_error():
    asset = _asset_handler(body=b"")
    with pytest.raises(EC.ExporterError, match="zero bytes"):
        _export(_client(asset=asset))


def test_non_svg_content_type_is_a_hard_error():
    """An unauthenticated or misrouted fetch comes back as an attachment
    download, not SVG bytes -- the wrong content-type must not be handed
    back to the caller as if it were the render."""
    asset = _asset_handler(content_type="application/octet-stream")
    with pytest.raises(EC.ExporterError, match="content-type"):
        _export(_client(asset=asset))


def test_asset_fetch_carries_the_auth_token_cookie():
    """`tempfile` is not in Penpot's public-buckets list -- the asset GET
    needs the same auth-token cookie as the export POST, not just the
    export POST."""
    seen = {}

    def asset(request: httpx.Request) -> httpx.Response:
        seen["cookie"] = request.headers.get("cookie", "")
        return httpx.Response(200, content=SVG_BYTES,
                              headers={"content-type": "image/svg+xml"})

    _export(_client(asset=asset))
    assert "auth-token=svc-token" in seen["cookie"]


def test_expired_tempfile_surfaces_as_a_failure_not_a_silent_retry():
    """Racing past Penpot's 10-minute tempfile expiry must be reported, not
    quietly retried -- the drawio session's client once read a transient
    failure as "unchanged" and left stale content on screen with nothing
    logged."""
    calls = {"asset": 0}

    def asset(request: httpx.Request) -> httpx.Response:
        calls["asset"] += 1
        return httpx.Response(404)

    with pytest.raises(EC.ExporterError, match="404"):
        _export(_client(asset=asset))
    assert calls["asset"] == 1


def test_asset_uri_that_does_not_point_at_the_frontend_is_refused():
    """Belt-and-braces on top of the 204 check: even if a misconfigured
    exporter handed back a backend-hosted URI, this client must refuse to
    fetch it rather than "helpfully" following it to the wrong host."""
    export = _export_ok_handler(uri="http://penpot-backend:6060/assets/by-id/x")
    with pytest.raises(EC.ExporterError, match="frontend"):
        _export(_client(export=export))


# --- auth ---------------------------------------------------------------

def test_401_on_export_triggers_exactly_one_relogin_then_succeeds():
    """A stale auth-token cookie must not become a bare failure, and must
    not become an infinite retry loop either -- exactly one re-login, then
    the original request completes."""
    calls = {"login": 0, "export": 0}
    tokens = iter(["first-token", "second-token"])

    def login(request: httpx.Request) -> httpx.Response:
        calls["login"] += 1
        token = next(tokens)
        body = EC._transit_dumps({EC.Keyword("id"): PROFILE_ID})
        return httpx.Response(200, text=body, headers={
            "content-type": "application/transit+json",
            "set-cookie": f"auth-token={token}; Path=/",
        })

    def export(request: httpx.Request) -> httpx.Response:
        calls["export"] += 1
        if calls["export"] == 1:
            return httpx.Response(401)
        assert "auth-token=second-token" in request.headers.get("cookie", "")
        body = EC._transit_dumps({EC.Keyword("uri"): ASSET_URI})
        return httpx.Response(200, text=body,
                              headers={"content-type": "application/transit+json"})

    data = _export(_client(login=login, export=export))
    assert data == SVG_BYTES
    assert calls["login"] == 2
    assert calls["export"] == 2


def test_a_second_401_after_relogin_is_not_retried_again():
    """One retry, never a loop -- a persistently-rejected service account
    must surface as an error."""
    def export_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    with pytest.raises(EC.ExporterError, match="401"):
        _export(_client(export=export_handler))


# --- explicit timeouts ---------------------------------------------------

def test_every_request_carries_an_explicit_non_none_timeout():
    """A wedged exporter must not pin a hub request forever -- see the
    shared client in awm/gateway/awm/gateway/hub/proxy.py, whose
    timeout=None is exactly the failure mode this refuses."""
    client = _client()
    seen_timeouts = []
    real_request = client._client.request

    def spy(method, url, **kwargs):
        seen_timeouts.append(kwargs.get("timeout"))
        return real_request(method, url, **kwargs)

    client._client.request = spy
    _export(client)

    assert len(seen_timeouts) >= 3  # login, export, asset
    assert all(t is not None for t in seen_timeouts)


# --- transit -------------------------------------------------------------

def test_transit_round_trips_keywords_uuids_maps_and_vectors():
    value = {
        EC.Keyword("cmd"): EC.Keyword("export-shapes"),
        EC.Keyword("profile-id"): PROFILE_ID,
        EC.Keyword("wait"): True,
        EC.Keyword("exports"): [
            {
                EC.Keyword("page-id"): uuidlib.UUID(PAGE_ID),
                EC.Keyword("scale"): 2,
                EC.Keyword("name"): "board",
            },
        ],
    }
    decoded = EC._transit_loads(EC._transit_dumps(value))
    assert decoded["cmd"] == "export-shapes"
    assert decoded["profile-id"] == PROFILE_ID
    assert decoded["wait"] is True
    export_entry = decoded["exports"][0]
    assert export_entry["page-id"] == uuidlib.UUID(PAGE_ID)
    assert export_entry["scale"] == 2
    assert export_entry["name"] == "board"


def test_encoded_export_body_uses_keyword_and_uuid_transit_forms():
    """The wire format itself, not just the round trip -- a decoder that
    happens to agree with its own encoder would miss a shape the real
    exporter refuses."""
    body = EC._transit_dumps({
        EC.Keyword("cmd"): EC.Keyword("export-shapes"),
        EC.Keyword("profile-id"): PROFILE_ID,
    })
    assert '"~:cmd"' in body
    assert '"~:export-shapes"' in body
    assert f'"~u{PROFILE_ID}"' in body


def test_transit_string_that_looks_like_an_escape_is_not_misread():
    """A literal string starting with '~' must round-trip as itself, not be
    mistaken for a keyword/uuid/escape tag."""
    decoded = EC._transit_loads(EC._transit_dumps({EC.Keyword("k"): "~odd"}))
    assert decoded["k"] == "~odd"


# --- the compact wire form Penpot actually answers in ----------------------
#
# Everything above this line round-trips through *this module's own* encoder,
# which writes maps as JSON objects. The live backend does not answer that way,
# and testing a codec only against itself is how a decoder ships broken. The
# two payloads below are real captures from the running instance.

LIVE_DEMO_PROFILE = (
    '["^ ","~:email","demo-1787813302687.demo@example.com",'
    '"~:password","_m5-kWrih0Ru5pHYXUizzw"]'
)

LIVE_LOGIN_RESPONSE = (
    '["^ ","~:is-admin",false,"~:email","demo-1787813302687.demo@example.com",'
    '"~:is-demo",true,"~:auth-backend","penpot",'
    '"~:fullname","Demo User 1787813302687","~:modified-at","~m1787813302844",'
    '"~:lang","","~:is-active",true,'
    '"~:default-project-id","~uf2de7aee-22fe-80f6-8008-8bc29d8ffee2",'
    '"~:id","~uf2de7aee-22fe-80f6-8008-8bc29d8eb1e7"]'
)


def test_a_map_arrives_as_an_array_not_a_json_object():
    """Penpot answers in transit's compact form, where a map is
    `["^ ", k, v, ...]`. A decoder that understands only JSON-object maps
    returns a *list* here, and `_login`'s `profile.get("id")` then fails on a
    response that was perfectly good."""
    decoded = EC._transit_loads(LIVE_DEMO_PROFILE)
    assert isinstance(decoded, dict)
    assert decoded["email"] == "demo-1787813302687.demo@example.com"


def test_the_real_login_response_yields_a_uuid_profile_id():
    """The exact bytes the live instance returned. `_login` reads `id` off
    this and requires a real UUID, so this is the end-to-end shape check."""
    decoded = EC._transit_loads(LIVE_LOGIN_RESPONSE)
    assert isinstance(decoded["id"], uuidlib.UUID)
    assert str(decoded["id"]) == "f2de7aee-22fe-80f6-8008-8bc29d8eb1e7"
    assert isinstance(decoded["default-project-id"], uuidlib.UUID)


def test_an_unknown_tag_in_an_ignored_field_does_not_fail_the_decode():
    """`~m<millis>` is a timestamp this client never reads. An unknown tag
    must pass through rather than abort a response whose other fields matter."""
    decoded = EC._transit_loads(LIVE_LOGIN_RESPONSE)
    assert decoded["modified-at"] == "~m1787813302844"


def test_a_repeated_key_arrives_as_a_cache_reference():
    """Transit caches strings of 4+ chars in parse order and spells later
    occurrences `^0`, `^1`, ... A decoder that ignores the cache reads the
    literal `"^0"` as a key and silently loses the real one."""
    doc = '[["^ ","~:name","a"],["^ ","^0","b"]]'
    first, second = EC._transit_loads(doc)
    assert first == {"name": "a"}
    assert second == {"name": "b"}


def test_a_cache_reference_past_the_end_is_refused_not_guessed():
    """A misaligned cache silently decodes the rest of the document into the
    wrong values, so an out-of-range index must fail loudly."""
    with pytest.raises(ValueError, match="cache reference"):
        EC._transit_loads('["^ ","^9","b"]')


def test_short_strings_are_not_cached_so_indices_stay_aligned():
    """The writer caches only strings of 4+ characters, counting the tag --
    so `~:a` (3) is not cached but `~:ab` (4) is. Admitting a shorter one
    shifts every later index: a whole-document corruption, not a local one."""
    doc = '[["^ ","~:a","x"],["^ ","~:name","y"],["^ ","^0","z"]]'
    first, second, third = EC._transit_loads(doc)
    assert first == {"a": "x"}
    assert second == {"name": "y"}
    # `~:a` is too short to cache, so ^0 must be `~:name`, not `~:a`.
    assert third == {"name": "z"}


# --- tagged values, captured from a real export ---------------------------

LIVE_EXPORT_RESPONSE = (
    '{"~:mtype":"image/svg+xml","~:name":"Thumbnail",'
    '"~:filename":"Thumbnail.svg",'
    '"~:id":"~u6d7e5d6d-88ab-8038-8008-8bc7f93fcfb8",'
    '"~:uri":{"~#uri":"http://localhost:9001/assets/by-id/'
    '1e35cd0e-422e-48c4-ad44-a8bf7ff6613e"}}'
)


def test_the_asset_uri_arrives_as_a_tagged_value_not_a_bare_string():
    """The exporter returns `{"~:uri": {"~#uri": "http://..."}}` -- a transit
    tagged value in verbose form. Left wrapped, the caller gets a dict where it
    expected a string and the export dies *after* a successful multi-second
    render, which is the most expensive place to fail. Real captured bytes."""
    decoded = EC._transit_loads(LIVE_EXPORT_RESPONSE)
    assert decoded["uri"] == (
        "http://localhost:9001/assets/by-id/1e35cd0e-422e-48c4-ad44-a8bf7ff6613e")
    assert decoded["mtype"] == "image/svg+xml"


def test_a_tagged_value_in_compact_form_unwraps_too():
    """The same value can arrive as `["~#tag", rep]` when the writer is not in
    verbose mode, so both spellings must reduce to the representation."""
    assert EC._transit_loads('["~#uri","http://x/y"]') == "http://x/y"


def test_a_two_element_map_is_not_mistaken_for_a_tagged_value():
    """Only a *single*-entry map whose key is a tag is a tagged value. A
    two-key map that happens to start with one must stay a map."""
    decoded = EC._transit_loads('["^ ","~:a",1,"~:b",2]')
    assert decoded == {"a": 1, "b": 2}


# --- sub-resource inlining ---------------------------------------------

def test_a_subresource_fetch_carries_the_auth_token_cookie():
    """Penpot's image assets are authenticated -- `/assets/by-file-media-id/...`
    answers 404 to an anonymous GET (verified live). Without the cookie every
    inlined image would silently become a 404 problem."""
    seen: list[str] = []

    def asset(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("cookie", ""))
        return httpx.Response(200, content=b"jpegbytes",
                              headers={"content-type": "image/jpeg"})

    client = _client(asset=asset)
    content_type, data = client.fetch_subresource(
        f"{BASE_URL}/assets/by-file-media-id/abc")
    assert (content_type, data) == ("image/jpeg", b"jpegbytes")
    assert any(f"{EC.COOKIE_NAME}=svc-token" in c for c in seen)


def test_a_cross_origin_subresource_is_refused_before_any_request():
    """The URL comes out of a user-authored document and this method attaches
    the service account's cookie to it. Anything that is not Penpot's own
    origin must be refused here, not filtered further up."""
    calls: list[str] = []

    def asset(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        calls.append(str(request.url))
        return httpx.Response(200, content=b"x", headers={"content-type": "text/plain"})

    client = _client(asset=asset)
    with pytest.raises(EC.ExporterError, match="neither the frontend base"):
        client.fetch_subresource("http://169.254.169.254/latest/meta-data/")
    assert calls == []


def test_an_oversized_subresource_is_refused():
    def asset(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 64,
                              headers={"content-type": "image/png"})

    client = _client(asset=asset)
    monkeyed = EC.MAX_SUBRESOURCE_BYTES
    EC.MAX_SUBRESOURCE_BYTES = 8
    try:
        with pytest.raises(EC.ExporterError, match="inlining cap"):
            client.fetch_subresource(f"{BASE_URL}/assets/by-file-media-id/big")
    finally:
        EC.MAX_SUBRESOURCE_BYTES = monkeyed


def test_a_failed_subresource_is_an_error_not_empty_bytes():
    def asset(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"")

    client = _client(asset=asset)
    with pytest.raises(EC.ExporterError, match="HTTP 404"):
        client.fetch_subresource(f"{BASE_URL}/assets/by-file-media-id/gone")


# --- Penpot's public origin vs the one we reach it on ----------------------

PUBLIC = "https://edge.example"


def test_an_asset_uri_on_penpots_public_origin_is_fetched_from_the_frontend():
    """Behind the edge, `PENPOT_PUBLIC_URI` must be the *edge's* origin or the
    browser's own API calls never reach Penpot. Penpot then stamps that origin
    onto the exporter's asset URI too -- correct for a browser, wrong for this
    service, which is on the container host. The path is what carries meaning,
    so the origin is normalised rather than refused."""
    seen: list[str] = []

    def asset(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=SVG_BYTES,
                              headers={"content-type": "image/svg+xml"})

    transport = httpx.MockTransport(_router(
        export=_export_ok_handler(uri=f"{PUBLIC}/assets/by-id/deadbeef"),
        asset=asset))
    client = EC.ExporterClient(base_url=BASE_URL, exporter_url=EXPORTER_URL,
                               public_uri=PUBLIC, username="svc-account",
                               password="hunter2", transport=transport)
    assert _export(client) == SVG_BYTES
    assert seen == [f"{BASE_URL}/assets/by-id/deadbeef"]


def test_a_third_origin_is_still_refused_when_a_public_uri_is_configured():
    """Normalising Penpot's own two origins must not become "fetch anything"."""
    client = EC.ExporterClient(base_url=BASE_URL, exporter_url=EXPORTER_URL,
                               public_uri=PUBLIC, username="svc-account",
                               password="hunter2",
                               transport=httpx.MockTransport(_router()))
    with pytest.raises(EC.ExporterError, match="neither the frontend base"):
        client.fetch_subresource("http://169.254.169.254/latest/meta-data/")


def test_a_subresource_on_the_public_origin_is_localised():
    seen: list[str] = []

    def asset(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=b"xy",
                              headers={"content-type": "image/jpeg"})

    client = EC.ExporterClient(base_url=BASE_URL, exporter_url=EXPORTER_URL,
                               public_uri=PUBLIC, username="svc-account",
                               password="hunter2",
                               transport=httpx.MockTransport(_router(asset=asset)))
    client.fetch_subresource(f"{PUBLIC}/internal/gfonts/font/a.woff2")
    assert seen == [f"{BASE_URL}/internal/gfonts/font/a.woff2"]


# --- the read cache counts tags too ---------------------------------------

def test_a_tag_occupies_a_read_cache_slot():
    """Transit caches a tag like any other `~`-prefixed string of four
    characters or more, so skipping it shifts every later back-reference by
    one. Found on a real 7.3 MB `get-file`, which decoded fine until a
    reference walked off the end of a cache 37 entries short; a small response
    never reaches far enough to notice."""
    payload = json.dumps(["^ ", "~:aaa", ["~#uri", "http://x"], "~:bbb", "^2"])
    decoded = EC._transit_loads(payload)
    assert decoded == {"aaa": "http://x", "bbb": "bbb"}


def test_a_verbose_tag_occupies_a_read_cache_slot():
    payload = json.dumps(["^ ", "~:aaa", {"~#uri": "http://x"}, "~:bbb", "^2"])
    decoded = EC._transit_loads(payload)
    assert decoded == {"aaa": "http://x", "bbb": "bbb"}


def test_a_two_element_vector_is_still_a_vector():
    """The tag check now decodes the head before judging it, so an ordinary
    two-element vector must survive as one rather than collapsing."""
    assert EC._transit_loads(json.dumps([1, 2])) == [1, 2]
    assert EC._transit_loads(json.dumps(["~:aaa", "~:bbb"])) == ["aaa", "bbb"]


def test_a_single_entry_map_that_is_not_a_tag_stays_a_map():
    assert EC._transit_loads(json.dumps({"~:aaa": 1})) == {"aaa": 1}


def test_the_real_get_file_payload_decodes():
    """The live payload that exposed the bug, in miniature: a tagged value
    ahead of the references that depend on the slot it takes."""
    payload = json.dumps(["^ ", "~:modified-at", {"~#uri": "http://x"},
                          "~:revn", 4, "~:name", "Tutorial", "^3", "again"])
    decoded = EC._transit_loads(payload)
    assert decoded["revn"] == 4
    # `^3` is the fourth cached string: modified-at, the ~#uri tag, revn,
    # name. Miss the tag and it lands on `revn` instead.
    assert decoded["name"] == "again"


def test_a_tag_shorter_than_the_cache_minimum_takes_no_slot():
    """`~#m` is three characters, so transit does not cache it -- the length
    rule applies to tags exactly as it does to keywords."""
    payload = json.dumps(["^ ", "~:aaa", {"~#m": 1}, "~:bbb", "^1"])
    assert EC._transit_loads(payload) == {"aaa": 1, "bbb": "bbb"}
