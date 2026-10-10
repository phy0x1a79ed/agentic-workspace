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
        else:
            assert policy.effects == {"read"}


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


# --- the delegate: an effect-based mode ---------------------------------------


def delegate(domain, verb, effect, call_args=None, peer=None):
    return refusal("delegate", domain, verb, peer, effect=effect, call_args=call_args)


@pytest.mark.parametrize("domain,verb", [("kb", "search"), ("cx", "list"), ("scope", "fetch"),
                                         ("transcripts", "search")])
def test_a_delegate_may_call_a_verb_that_declares_read(domain, verb):
    assert delegate(domain, verb, "read") is None


@pytest.mark.parametrize("effect", ["write", "queue", "secret", None])
@pytest.mark.parametrize("domain,verb", [("scope", "post"), ("cx", "start"), ("cx", "stop"),
                                         ("kb", "add"), ("artifact", "publish"),
                                         ("precedence", "record"), ("scope", "sync"),
                                         ("reflection", "send")])
def test_a_delegate_is_refused_every_other_effect_and_an_undeclared_one(domain, verb, effect):
    assert delegate(domain, verb, effect) is not None


def test_a_secret_effect_is_refused_even_when_a_verb_is_otherwise_a_read():
    assert delegate("auth", "token", "secret") is not None


@pytest.mark.parametrize("verb", ["browser_cdp", "browser_open", "factorio_run"])
def test_the_whole_rlm_domain_is_refused_whatever_it_declares(verb):
    for effect in ("read", "write", None):
        assert delegate("rlm", verb, effect) is not None


def test_the_door_is_refused_even_though_its_verbs_are_reads():
    for verb in ("status", "list", "get"):
        assert delegate("door", verb, "read") is not None
    assert delegate("door", "assign", "write") is not None


def test_a_delegate_finishes_a_card_and_reads_the_board():
    for verb in ("get", "list", "complete", "fail"):
        assert delegate("board", verb, "queue") is None
        assert delegate("board", verb, None) is None
    assert delegate("board", "claim", "queue") is not None
    assert delegate("board", "party_list", "read") is not None
    assert delegate("board", "party_add", "secret") is not None


def test_a_delegate_may_compact_and_identify_itself_but_not_send_or_change_mode():
    for verb in ("compact", "whoami"):
        assert delegate("reflection", verb, None) is None
        assert delegate("reflection", verb, "write") is None
    for verb in ("send", "mode", "pending"):
        assert delegate("reflection", verb, None) is not None
        assert delegate("reflection", verb, "write") is not None
    assert delegate("reflection", "compact", None, peer="capella") is not None


def test_a_delegate_posts_only_a_reply_message():
    reply = {"kind": "message", "reply_to": "a" * 32, "title": "t", "body": "b"}
    assert delegate("board", "post", "queue", reply) is None
    for bad in ({"kind": "request", "reply_to": "a" * 32}, {"kind": "message"},
                {"kind": "message", "reply_to": ""}, {"kind": "message", "reply_to": 7},
                {"reply_to": "a" * 32}, {}):
        assert delegate("board", "post", "queue", bad) is not None, bad
    assert delegate("board", "post", "queue", None) is not None


@pytest.mark.parametrize("domain,verb", [("kb", "search"), ("board", "complete")])
def test_a_delegate_may_not_name_a_peer(domain, verb):
    assert "peer" in delegate(domain, verb, "read", peer="capella")


def test_a_delegate_may_describe_every_domain_but_the_denied_ones():
    assert refusal("delegate", "board", "describe") is None
    assert refusal("delegate", "cx", "describe") is None
    assert refusal("delegate", "rlm", "describe") is not None
    assert refusal("delegate", "door", "describe") is not None


def test_the_effect_policy_admits_and_denies_as_declared():
    policy = modes.ByEffect(
        effects=frozenset({"read"}), denied_domains=frozenset({"a"}),
        denied_pairs=frozenset({("b", "x")}), pairs=frozenset({("c", "w")}),
        when={("d", "v"): lambda a: a.get("ok") is True})
    assert policy.admits("e", "r", "read", None) and not policy.admits("e", "r", "write", None)
    assert not policy.admits("a", "r", "read", None)
    assert not policy.admits("b", "x", "read", None) and policy.admits("b", "y", "read", None)
    assert policy.admits("c", "w", "write", None)
    assert policy.admits("d", "v", "write", {"ok": True})
    assert not policy.admits("d", "v", "read", {"ok": False})


def test_reserved_names_and_modes():
    assert {"representative", "secretary", "delegate"} == set(modes.RESERVED_MODES)
    assert {"representative", "secretary"} == set(modes.RESERVED_NAMES)
