"""Trust per session: start, raise, steer, cross-edge and journal bounds."""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

import verify_grant

import awm.trustpolicy as tp

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

NOW = 1_800_000_000


def _key():
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return sk, pk


def _grant(sk, pk, **over):
    payload = over.pop("payload", {"session_id": "s1", "from": "cooperative", "to": "trusted"})
    grant = {"grant_id": "g1", "request_id": "r1", "kind": "trust_raise", "node": "home",
             "subject": "s1", "payload_sha256": tp.payload_digest(payload),
             "not_before": NOW - 10, "not_after": NOW + 900, "approved_by_device": "pk1"}
    grant.update(over)
    sig = base64.urlsafe_b64encode(sk.sign(tp.canonical(grant))).decode().rstrip("=")
    return {"grant": grant, "sig": sig, "key_id": verify_grant.key_id(Ed25519PublicKey.from_public_bytes(pk))}


def test_a_starter_sets_equal_or_lower():
    assert tp.level_for_start("trusted") == "trusted"
    assert tp.level_for_start("trusted", "untrusted") == "untrusted"
    assert tp.level_for_start("cooperative", "cooperative") == "cooperative"


def test_a_raise_without_a_grant_is_refused():
    with pytest.raises(tp.TrustError, match="grant"):
        tp.level_for_start("cooperative", "trusted", session_id="s1")


def test_a_raise_with_tonys_grant_passes():
    sk, pk = _key()
    level = tp.level_for_start("cooperative", "trusted", session_id="s1",
                               grant=_grant(sk, pk), grant_key=pk, node="home", now=NOW)
    assert level == "trusted"


@pytest.mark.parametrize("bad", [
    {"subject": "s2"},
    {"kind": "invite"},
    {"node": "other"},
    {"not_after": NOW - 1},
    {"payload": {"session_id": "s1", "from": "untrusted", "to": "trusted"}},
])
def test_a_grant_for_something_else_is_refused(bad):
    sk, pk = _key()
    with pytest.raises(tp.TrustError):
        tp.level_for_start("cooperative", "trusted", session_id="s1",
                           grant=_grant(sk, pk, **bad), grant_key=pk, node="home", now=NOW)


def test_a_forged_grant_is_refused():
    sk, pk = _key()
    other, _ = _key()
    env = _grant(other, pk)
    with pytest.raises(tp.TrustError, match="signature"):
        tp.level_for_start("cooperative", "trusted", session_id="s1",
                           grant=env, grant_key=pk, node="home", now=NOW)
    env = _grant(sk, pk)
    env["grant"]["subject"] = "s1"
    env["grant"]["not_after"] += 1
    with pytest.raises(tp.TrustError, match="signature"):
        tp.level_for_start("cooperative", "trusted", session_id="s1",
                           grant=env, grant_key=pk, node="home", now=NOW)


def test_untrusted_steers_and_messages_nobody():
    for verb in ("start_other", "message", "stop", "interrupt", "cd"):
        ok, why = tp.may_steer("untrusted", verb)
        assert not ok and "untrusted" in why


def test_nobody_steers_above_its_level():
    assert tp.may_steer("cooperative", "stop", target_level="trusted")[0] is False
    assert tp.may_steer("cooperative", "mcp", target_level="trusted")[0] is False
    assert tp.may_steer("cooperative", "stop", target_level="cooperative")[0] is True
    assert tp.may_steer("trusted", "interrupt", target_level="trusted")[0] is True


def test_a_message_may_go_up():
    assert tp.may_steer("cooperative", "message", target_level="trusted") == (True, "")


def test_cross_edge_follows_the_edges():
    edges = {("home", "work")}
    reaches = lambda a, b: (a, b) in edges  # noqa: E731
    assert tp.cross_edge("home", "work", "cooperative", reaches) == (True, "cooperative")
    ok, why = tp.cross_edge("work", "home", "trusted", reaches)
    assert not ok and "no edge" in why
    assert tp.cross_edge("home", "work", "untrusted", reaches)[0] is False


def test_journal_bounds_by_level():
    reachable = lambda n: ["work"] if n == "home" else []  # noqa: E731
    t = tp.journal_bound("trusted", node="home", project="awm", scope="x", reachable=reachable)
    assert t.may_read("work", "cloud", "y") and t.may_write("cloud", "y")
    assert not t.may_read("relay", "awm", "x")
    c = tp.journal_bound("cooperative", node="home", project="awm", scope="x")
    assert c.may_read("home", "cloud", "y") and not c.may_read("work", "awm", "x")
    assert c.may_write("awm", "x") and not c.may_write("awm", "y")
    u = tp.journal_bound("untrusted", node="home", project="awm", scope="x")
    assert u.may_read("home", "awm", "x") and not u.may_read("home", "awm", "y")
    assert not u.may_write("cloud", "y")


def test_unknown_level_is_an_error():
    with pytest.raises(ValueError):
        tp.level_for_start("root")
