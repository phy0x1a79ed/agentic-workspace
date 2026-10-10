"""The session mode policies and the refusal decision."""

from __future__ import annotations

import pytest

from awm.config import modes
from awm.config.modes import MODES, refusal


def allowed(mode):
    return {f"{d}.{v}" for d, v in MODES[mode].pairs}


def test_the_representative_allowlist_is_exactly_its_verbs():
    assert allowed("representative") == {
        "board.list", "board.get", "board.fail", "cx.start", "cx.list",
        "reflection.compact", "reflection.whoami",
        "door.status", "door.list", "door.get", "door.assign",
        "scope.fetch", "scope.search", "scope.goal_read", "scope.goal_history",
        "scope.resolve", "project.search",
    }


def test_the_secretary_allowlist_is_exactly_its_verbs():
    assert allowed("secretary") == {
        "cx.start", "cx.list", "cx.stop", "board.list", "board.get",
        "door.status", "door.list", "door.get", "reflection.compact",
        "reflection.whoami", "scope.fetch", "scope.search", "scope.goal_read",
        "scope.goal_history", "scope.resolve", "project.search",
    }


def test_the_representative_never_claims_completes_or_posts():
    for verb in ("board.post", "board.claim", "board.complete", "scope.refresh"):
        assert verb not in allowed("representative")
        assert verb not in allowed("secretary")


def test_the_unknown_mode_admits_only_reflection():
    assert {d for d, _ in MODES[modes.UNKNOWN].pairs} == {"reflection"}
    assert "reflection.mode" not in allowed(modes.UNKNOWN)


def test_no_allow_mode_can_change_its_own_permission_mode_or_send_text():
    for mode, policy in MODES.items():
        if isinstance(policy, modes.Allow):
            assert "reflection.mode" not in allowed(mode)
            assert "reflection.send" not in allowed(mode)


def test_the_secretary_never_writes_to_the_board_or_the_queue():
    writes = {"board.post", "board.claim", "board.complete", "board.fail", "door.assign"}
    assert not writes & allowed("secretary")


def test_only_listed_modes_are_restricted():
    for mode in ("representative", "secretary", "delegate", "unknown"):
        assert modes.is_restricted(mode)
    assert not modes.is_restricted(None)
    assert not modes.is_restricted("worker")


def test_an_unrestricted_mode_has_no_refusal():
    assert refusal(None, "ssh", "connect") is None
    assert refusal("worker", "scope", "post", "capella") is None


def test_an_allowed_pair_passes_and_others_are_refused():
    assert refusal("representative", "board", "list") is None
    assert "board.party_add" in refusal("representative", "board", "party_add")
    assert refusal("secretary", "cx", "stop") is None
    assert refusal("representative", "cx", "stop") is not None


@pytest.mark.parametrize("mode", ["representative", "delegate"])
@pytest.mark.parametrize("domain,verb", [(None, "list"), ("board", None), ("", "list"),
                                         ("board", ""), ("board", 3), (4, "list")])
def test_a_call_without_a_domain_and_verb_is_refused(mode, domain, verb):
    assert refusal(mode, domain, verb) is not None


@pytest.mark.parametrize("mode", ["representative", "delegate", "unknown"])
def test_a_peer_is_refused_even_for_an_allowed_verb(mode):
    domain, verb = ("reflection", "compact") if mode == "unknown" else ("board", "list")
    assert "peer" in refusal(mode, domain, verb, "capella")


def test_describe_needs_a_domain_the_mode_can_use():
    assert refusal("representative", "board", "describe") is None
    assert refusal("representative", "ssh", "describe") is not None
    assert refusal("representative", "board", "describe", "capella") is not None


# --- the delegate: a deny-style mode ------------------------------------------


DELEGATE_DENIED = [
    ("cx", "start"), ("cx", "stop"), ("cx", "claim"), ("cx", "seed"), ("cx", "remove"),
    ("ssh", "connect"), ("auth", "login"), ("peer", "list"), ("config", "set"),
    ("vpn", "up"), ("2fa", "approve"), ("tether", "send"), ("social", "send"),
    ("httpsfront", "start"), ("gateway", "restart"), ("services", "stop"),
    ("services", "restart"), ("board", "party_add"), ("board", "party_revoke"),
    ("board", "party_list"), ("reflection", "send"), ("reflection", "mode"),
    ("dsh", "run"), ("compute", "submit"), ("dev", "shadow"),
    ("scope", "delete"), ("scope", "data_gc"),
]
DELEGATE_ALLOWED = [
    ("cx", "list"), ("board", "list"), ("board", "get"), ("board", "post"),
    ("board", "complete"), ("board", "fail"), ("board", "claim"),
    ("scope", "post"), ("scope", "fetch"), ("scope", "create"), ("scope", "goal_set"),
    ("kb", "search"), ("artifact", "publish"), ("reflection", "compact"),
    ("reflection", "whoami"), ("transcripts", "search"), ("project", "search"),
]


@pytest.mark.parametrize("domain,verb", DELEGATE_DENIED)
def test_a_delegate_is_refused_what_the_deny_set_names(domain, verb):
    assert refusal("delegate", domain, verb) is not None


@pytest.mark.parametrize("domain,verb", DELEGATE_ALLOWED)
def test_a_delegate_may_call_everything_else(domain, verb):
    assert refusal("delegate", domain, verb) is None


def test_a_delegate_may_describe_a_domain_it_can_partly_use():
    assert refusal("delegate", "board", "describe") is None
    assert refusal("delegate", "cx", "describe") is None  # cx list is kept
    assert refusal("delegate", "ssh", "describe") is not None


def test_a_deny_policy_admits_and_denies_as_declared():
    policy = modes.Deny(domains=frozenset({"a"}), pairs=frozenset({("b", "x")}),
                        keep=frozenset({("a", "ok")}))
    assert policy.admits("a", "ok") and not policy.admits("a", "no")
    assert not policy.admits("b", "x") and policy.admits("b", "y")
    assert policy.admits("c", "z")


def test_reserved_names_and_modes():
    assert {"representative", "secretary", "delegate"} == set(modes.RESERVED_MODES)
    assert {"representative", "secretary"} == set(modes.RESERVED_NAMES)
