"""The device-token codec: shape, parse and the digest the edge compares."""

from __future__ import annotations

import base64

import pytest

from awm.config import peertoken, usertoken

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def test_a_minted_token_has_the_pinned_shape():
    token_id, secret, token = usertoken.mint()
    assert token == f"awmut1.{token_id}.{secret}"
    assert len(token_id) == 16 and int(token_id, 16) >= 0
    assert "=" not in secret
    assert len(base64.urlsafe_b64decode(secret + "=")) == 32
    assert usertoken.parse(token) == (token_id, secret)


@pytest.mark.parametrize("bad", [
    "", "awmut1.", "awmut1.0123456789abcdef", "awmut1.0123456789abcdef.",
    "awmut1.0123456789ABCDEF." + "a" * 43, "awmut1.0123." + "a" * 43,
    "awmut1.0123456789abcdef." + "a" * 42, "awmut1.0123456789abcdef." + "a" * 44,
    "awmut1.0123456789abcdef." + "a" * 42 + "=", "awmut1.0123456789abcdef.x.y",
    "awmpt1.0123456789abcdef." + "a" * 43,
])
def test_a_malformed_token_does_not_parse(bad):
    with pytest.raises(usertoken.TokenError):
        usertoken.parse(bad)


def test_the_digest_matches_only_its_secret():
    _, secret, _ = usertoken.mint()
    stored = usertoken.digest(secret)
    assert usertoken.matches(secret, stored)
    assert not usertoken.matches(usertoken.mint()[1], stored)
    assert not usertoken.matches(secret, "")
    assert not usertoken.matches(secret, "ünicode")


def test_the_two_token_kinds_never_look_alike():
    _, _, token = usertoken.mint()
    assert usertoken.looks_like_token(token) and not peertoken.looks_like_token(token)
    node = peertoken.sign(peertoken.generate_private_key(), iss="a", aud="b")
    assert not usertoken.looks_like_token(node)
