"""Which paths the tether mount claims, and what the public door says about them.

This mount is the widest thing on the public edge — every path it claims is
reachable with no awm session at all — so what it claims is the whole of the
decision. These pin it as a closed set of exact shapes rather than a prefix: a
slot the relay never issued, a ticket of the wrong length, or a name merely
starting with the eight characters of the mount are all turned away here,
before anything is proxied and before any session is opened.

The grammars must agree exactly with what the relay parses, in
``tether-proto``'s ``Slot::parse`` (one to three digits, no leading zero, 1 to
999) and ``tether-relay``'s ``Token::parse`` (thirty-two lowercase hex). The
boundaries below are those two definitions written out, so a change on either
side has to change this file too.
"""

from __future__ import annotations

import pytest

from awm.httpsfront import policy, tether

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

TICKET = "0123456789abcdef0123456789abcdef"


# -- what the mount claims ----------------------------------------------------

@pytest.mark.parametrize("path", [
    "/tether",                                  # the launcher, piped into a shell
    "/tether/win",                              # the same launcher, for Windows
    "/tether/bin/tether",                       # a client download
    "/tether/bin/tether-windows-x86_64.exe",
    "/tether/bin/tether-macos-arm64",
    "/tether/claim/1",                          # the owner's ticket
    "/tether/claim/999",
    f"/tether/join/7/{TICKET}",                 # the session socket
    "/tether/issue",                            # the operator's, gated at the relay
    "/tether/status",
])
def test_the_mount_claims_the_relays_whole_public_surface(path):
    assert tether.owns(path)
    assert not tether.refused(path)
    assert policy.classify(path) is policy.Verdict.TETHER
    # No session, by design: the person redeeming an invite has no awm account
    # and the tool would be useless to them if they needed one.
    assert policy.allows(path, None)


@pytest.mark.parametrize("path,why", [
    ("/tether/health", "the relay's liveness probe, which is loopback's business"),
    ("/tether/", "a trailing slash is not a route the relay has"),
    ("/tether/claim/0", "slot 0 is not a slot"),
    ("/tether/claim/07", "a leading zero is a different string for the same number"),
    ("/tether/claim/1000", "above the highest slot the relay will issue"),
    ("/tether/claim/7/extra", "a claim takes one segment"),
    ("/tether/claim", "a claim names a slot"),
    (f"/tether/join/7/{TICKET[:31]}", "a ticket is thirty-two hex"),
    (f"/tether/join/7/{TICKET.upper()}", "the relay parses lowercase hex"),
    ("/tether/join/7", "a join names a slot and a ticket"),
    ("/tether/bin/../../etc/passwd", "a download names one file"),
    ("/tether/bin/", "a download names a file"),
    ("/tether/win/", "a trailing slash is not a route the relay has"),
    ("/tether/windows", "the Windows launcher is at /win and only there"),
    ("/tether/invoke", "nothing else on the relay is a route at all"),
])
def test_a_near_miss_inside_the_mount_is_refused_rather_than_forwarded(path, why):
    assert not tether.owns(path), why
    assert tether.refused(path), why
    # DENY and not a fall-through: without the `refused` branch this would be
    # proxied to the *gateway*, which is a different and much worse answer.
    assert policy.classify(path) is policy.Verdict.DENY, why
    assert not policy.allows(path, None)
    assert not policy.allows(path, "tony")


@pytest.mark.parametrize("path", ["/tether-admin", "/tetherfoo", "/ui/tether"])
def test_a_name_that_merely_starts_the_same_is_not_this_mounts(path):
    """The mount is claimed by exact shape, so a future page called something
    beginning with these eight characters is still the gateway's — and reaching
    the internet by having a name is the one thing the public door exists to
    prevent."""
    assert not tether.owns(path)
    assert not tether.refused(path)


# -- the rewrite --------------------------------------------------------------

@pytest.mark.parametrize("path,inner", [
    ("/tether", "/"),
    ("/tether/claim/7", "/claim/7"),
    (f"/tether/join/7/{TICKET}", f"/join/7/{TICKET}"),
    ("/tether/issue", "/issue"),
    ("/tether/bin/tether", "/bin/tether"),
    ("/tether/win", "/win"),
])
def test_the_mount_comes_off_the_same_way_in_text_and_in_bytes(path, inner):
    assert tether.upstream_path(path) == inner
    assert tether.upstream_raw_path(path.encode()) == inner.encode()


def test_the_bare_mount_is_the_launcher_and_that_is_why_the_relay_serves_root():
    """The address a person is read out is the mount itself. It has to reach
    the relay as a path the relay answers, which is why the relay serves the
    launcher at its root as well as at /tether."""
    assert tether.upstream_path(tether.PREFIX) == "/"


def test_a_mount_that_is_only_there_after_decoding_is_not_the_mount():
    """The edge routes on the decoded path and forwards the raw one. A target
    whose prefix appears only once decoded would classify as this mount's and
    then be forwarded with the prefix still attached."""
    assert tether.upstream_raw_path(b"/%74ether/claim/7") is None


# -- what is recorded ---------------------------------------------------------

def test_every_route_we_decline_to_forward_is_named_with_its_reason():
    for path, reason in tether.NOT_FORWARDED.items():
        assert reason, path
        assert tether.refused(tether.PREFIX + path), path
