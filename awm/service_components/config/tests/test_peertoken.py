"""Node tokens: sign, verify, and the four ways a token must be refused."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from awm.config import peertoken

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

NOW = 1_800_000_000


@pytest.fixture()
def keys():
    private = peertoken.generate_private_key()
    return private, peertoken.public_key_of(private)


def _token(private, **kw):
    kw.setdefault("iss", "altair")
    kw.setdefault("aud", "mira")
    kw.setdefault("now", NOW)
    return peertoken.sign(private, **kw)


def test_round_trip(keys):
    private, public = keys
    claims = peertoken.verify(_token(private), public, aud="mira", now=NOW + 1)
    assert claims == {"iss": "altair", "aud": "mira", "iat": NOW, "exp": NOW + 300}


def test_token_is_prefixed_and_compact(keys):
    token = _token(keys[0])
    assert token.startswith("awmpt1.")
    assert token.count(".") == 2
    assert peertoken.looks_like_token(token)
    assert not peertoken.looks_like_token("some-legacy-bearer")
    assert peertoken.issuer_of(token) == "altair"


def test_wrong_audience_is_refused(keys):
    private, public = keys
    with pytest.raises(peertoken.TokenError, match="audience"):
        peertoken.verify(_token(private, aud="shaula"), public, aud="mira", now=NOW)


def test_expired_token_is_refused_after_the_skew(keys):
    private, public = keys
    token = _token(private)
    peertoken.verify(token, public, aud="mira", now=NOW + 300 + 59)
    with pytest.raises(peertoken.TokenError, match="expired"):
        peertoken.verify(token, public, aud="mira", now=NOW + 300 + 61)


def test_clock_skew_admits_a_slightly_future_token(keys):
    private, public = keys
    token = _token(private)
    peertoken.verify(token, public, aud="mira", now=NOW - 59)
    with pytest.raises(peertoken.TokenError, match="future"):
        peertoken.verify(token, public, aud="mira", now=NOW - 61)


def test_wrong_key_is_refused(keys):
    private, _ = keys
    other_public = peertoken.public_key_of(peertoken.generate_private_key())
    with pytest.raises(peertoken.TokenError, match="signature"):
        peertoken.verify(_token(private), other_public, aud="mira", now=NOW)


def test_tampered_payload_is_refused(keys):
    private, public = keys
    prefix, payload, signature = _token(private).split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["iss"] = "shaula"
    forged = base64.urlsafe_b64encode(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    ).rstrip(b"=").decode()
    with pytest.raises(peertoken.TokenError, match="signature"):
        peertoken.verify(f"{prefix}.{forged}.{signature}", public, aud="mira", now=NOW)


def test_tampered_signature_is_refused(keys):
    private, public = keys
    prefix, payload, signature = _token(private).split(".")
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    with pytest.raises(peertoken.TokenError):
        peertoken.verify(f"{prefix}.{payload}.{flipped}", public, aud="mira", now=NOW)


def test_overlong_lifetime_is_refused(keys):
    private, public = keys
    with pytest.raises(peertoken.TokenError, match="lifetime"):
        peertoken.verify(_token(private, ttl=3600), public, aud="mira", now=NOW)


@pytest.mark.parametrize("junk", ["", "awmpt1.", "awmpt1.a", "awmpt1.a.b.c",
                                  "bearer-xyz", "awmpt1.!!!.???"])
def test_junk_is_refused_not_raised_as_something_else(keys, junk):
    with pytest.raises(peertoken.TokenError):
        peertoken.verify(junk, keys[1], aud="mira", now=NOW)


def test_issuer_of_junk_is_none():
    assert peertoken.issuer_of("nope") is None
    assert peertoken.issuer_of("awmpt1.zzz.zzz") is None


def test_fingerprint_is_ssh_style_over_the_raw_key(keys):
    _, public = keys
    raw = base64.b64decode(public)
    expected = "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
    assert peertoken.fingerprint(public) == expected
    assert "=" not in expected


def test_public_key_is_accepted_unpadded_and_urlsafe(keys):
    private, public = keys
    token = _token(private)
    loose = public.rstrip("=").replace("+", "-").replace("/", "_")
    peertoken.verify(token, loose, aud="mira", now=NOW)
    assert peertoken.fingerprint(loose) == peertoken.fingerprint(public)


def test_a_key_of_the_wrong_length_is_refused():
    with pytest.raises(peertoken.TokenError):
        peertoken.fingerprint(base64.b64encode(b"short").decode())
