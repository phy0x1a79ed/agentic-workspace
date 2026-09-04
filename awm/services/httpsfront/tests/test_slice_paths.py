"""Which paths belong to a slice, and what a slice may ask the vault for.

What these pin: the token is a path segment and only a token-shaped one counts;
the mount is stripped from the decoded path and from the raw bytes alike; a
slice inherits every refusal the vault records and adds its own; and the public
door admits a slice with no session while still refusing the paths inside it
that the vault refuses.
"""

from __future__ import annotations

import pytest

from awm.httpsfront import policy, slices, vault

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

TOKEN = "Ab3-_9xYzQw012345678"


def _p(inner: str = "/") -> str:
    return slices.PREFIX + TOKEN + inner


# -- the token is a path segment ---------------------------------------------

def test_the_token_is_the_segment_after_the_mount():
    assert slices.token_of(_p("/api/tree")) == TOKEN
    assert slices.token_of(_p()) == TOKEN
    assert slices.token_of(slices.shell_bare(TOKEN)) == TOKEN


def test_a_segment_that_is_not_token_shaped_names_no_slice():
    # Anything we did not mint is not a slice, so it is not the vault's either:
    # it falls through the door to DENY like any unlisted path.
    for bad in ("/slice/", "/slice/short", "/slice/has.a.dot0123456789",
                "/slice/" + "x" * 200):
        assert slices.token_of(bad) is None
        assert not slices.owns(bad)
        assert policy.classify(bad) is policy.Verdict.DENY


# -- the rewrite --------------------------------------------------------------

def test_the_mount_and_the_token_are_stripped_and_nothing_else_is():
    assert slices.upstream_path(_p()) == "/"
    assert slices.upstream_path(slices.shell_bare(TOKEN)) == "/"
    assert slices.upstream_path(_p("/api/tree")) == "/api/tree"
    assert slices.upstream_path("/trilium/api/tree") == "/trilium/api/tree"


def test_the_raw_bytes_carry_the_mount_or_the_target_is_refused():
    assert slices.upstream_raw_path(_p("/api/search/%23foo").encode()) == \
        b"/api/search/%23foo"
    assert slices.upstream_raw_path(slices.shell_bare(TOKEN).encode()) == b"/"
    # Route said yes, bytes say no: the caller answers 404, as it does for the
    # vault's own mount.
    assert slices.upstream_raw_path(b"/%73lice/" + TOKEN.encode()) is None
    assert slices.upstream_raw_path(b"/trilium/api/tree") is None


# -- what a slice may not ask for --------------------------------------------

def test_a_slice_inherits_every_refusal_the_vault_records():
    for entry in vault.NOT_FORWARDED:
        assert entry in slices.ALL_NOT_FORWARDED
        inner = entry + "x" if entry.endswith("/") else entry
        assert not slices.owns(_p(inner))


def test_a_slice_refuses_what_only_an_account_holder_could_want():
    assert not slices.owns(_p("/manifest.webmanifest"))
    assert not slices.owns(_p("/robots.txt"))
    # The shell and its assets are the point, and they still pass.
    assert slices.owns(_p())
    assert slices.owns(_p("/api/tree"))
    assert slices.owns(_p("/icon.png"))


# -- the public door ----------------------------------------------------------

def test_the_door_admits_a_slice_with_no_session_at_all():
    assert policy.classify(_p()) is policy.Verdict.SLICE
    assert policy.allows(_p(), None)
    assert policy.allows(_p("/api/tree"), None)


def test_the_door_still_refuses_the_paths_inside_a_slice_that_it_refuses():
    assert policy.classify(_p("/etapi/app-info")) is policy.Verdict.DENY
    assert not policy.allows(_p("/etapi/app-info"), None)
    assert not policy.allows(_p("/etapi/app-info"), "tony")


def test_the_vault_is_untouched_by_the_new_verdict():
    assert policy.classify(vault.SHELL) is policy.Verdict.VAULT
    assert not policy.allows(vault.SHELL, None)
    assert policy.allows(vault.SHELL, "tony")
