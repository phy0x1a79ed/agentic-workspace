"""The session mode policies and the refusal decision, over test policies and the one built-in mode."""

from __future__ import annotations

import pytest

from awm.config import modes
from awm.config.modes import MODES, Allow, ByEffect, refusal


def pairs(*items):
    return frozenset(tuple(i.split(".")) for i in items)


@pytest.fixture(autouse=True)
def test_modes(monkeypatch):
    """A fixed-job mode and an effect-based mode, registered beside `unknown`."""
    monkeypatch.setitem(MODES, "fixed", Allow(pairs("operations.board_list", "reflection.compact")))
    monkeypatch.setitem(MODES, "reader", ByEffect(
        effects=frozenset({"read"}),
        denied_domains=frozenset({"browser"}),
        denied_pairs=pairs("operations.party_list"),
        pairs=pairs("operations.board_complete"),
        when={("operations", "board_post"): lambda args: args.get("kind") == "message"}))


def reader(domain, verb, effect, call_args=None, peer=None):
    return refusal("reader", domain, verb, peer, effect=effect, call_args=call_args)


def test_the_built_in_modes_are_only_unknown():
    assert [m for m in MODES if m not in ("fixed", "reader")] == [modes.UNKNOWN]
    assert not {"REPRESENTATIVE", "SECRETARY", "DELEGATE", "RESERVED_MODES", "RESERVED_NAMES",
                "DELEGATING_MODES"} & set(dir(modes))


def test_the_unknown_mode_admits_only_reflection_compact_and_whoami():
    assert MODES[modes.UNKNOWN].pairs == pairs("reflection.compact", "reflection.whoami")
    assert refusal(modes.UNKNOWN, "reflection", "compact") is None
    assert refusal(modes.UNKNOWN, "reflection", "mode") is not None
    assert refusal(modes.UNKNOWN, "operations", "task_claim", effect="write") is not None


def test_only_listed_modes_are_restricted():
    assert modes.is_restricted("unknown") and modes.is_restricted("fixed")
    assert not modes.is_restricted(None)
    assert not modes.is_restricted("worker")
    assert not modes.is_restricted("representative"), "the old persona modes are plain labels now"


def test_an_unrestricted_mode_has_no_refusal():
    assert refusal(None, "ssh", "connect") is None
    assert refusal("worker", "journal", "post", "capella") is None


def test_an_allowed_pair_passes_and_others_are_refused():
    assert refusal("fixed", "operations", "board_list") is None
    assert "operations.party_add" in refusal("fixed", "operations", "party_add")
    assert refusal("fixed", "reflection", "stop_other") is not None


@pytest.mark.parametrize("mode", ["fixed", "reader"])
@pytest.mark.parametrize("domain,verb", [(None, "list"), ("operations", None), ("", "list"),
                                         ("operations", ""), ("operations", 3), (4, "list")])
def test_a_call_without_a_domain_and_verb_is_refused(mode, domain, verb):
    assert refusal(mode, domain, verb) is not None


@pytest.mark.parametrize("mode", ["fixed", "reader", "unknown"])
def test_a_peer_is_refused_even_for_an_allowed_verb(mode):
    domain, verb = ("reflection", "compact") if mode == "unknown" else ("operations", "board_list")
    assert "peer" in refusal(mode, domain, verb, "capella")


def test_describe_needs_a_domain_the_mode_can_use():
    assert refusal("fixed", "operations", "describe") is None
    assert refusal("fixed", "ssh", "describe") is not None
    assert refusal("fixed", "operations", "describe", "capella") is not None
    assert refusal("reader", "kb", "describe") is None
    assert refusal("reader", "browser", "describe") is not None


@pytest.mark.parametrize("domain,verb", [("kb", "search"), ("reflection", "list_other"),
                                         ("journal", "fetch")])
def test_an_effect_mode_may_call_a_verb_that_declares_read(domain, verb):
    assert reader(domain, verb, "read") is None


@pytest.mark.parametrize("effect", ["write", "queue", "secret", None])
def test_an_effect_mode_refuses_every_other_effect_and_an_undeclared_one(effect):
    assert reader("journal", "post", effect) is not None


def test_a_denied_domain_or_pair_is_refused_whatever_it_declares():
    for effect in ("read", "write", None):
        assert reader("browser", "cdp", effect) is not None
    assert reader("operations", "party_list", "read") is not None


def test_a_named_pair_passes_whatever_it_declares():
    for effect in ("queue", "write", None):
        assert reader("operations", "board_complete", effect) is None


def test_a_conditional_pair_passes_only_when_the_calls_arguments_pass():
    assert reader("operations", "board_post", "queue", {"kind": "message"}) is None
    for bad in ({"kind": "request"}, {}, None):
        assert reader("operations", "board_post", "queue", bad) is not None


def test_the_effect_policy_admits_and_denies_as_declared():
    policy = ByEffect(
        effects=frozenset({"read"}), denied_domains=frozenset({"a"}),
        denied_pairs=frozenset({("b", "x")}), pairs=frozenset({("c", "w")}),
        when={("d", "v"): lambda a: a.get("ok") is True})
    assert policy.admits("e", "r", "read", None) and not policy.admits("e", "r", "write", None)
    assert not policy.admits("a", "r", "read", None)
    assert not policy.admits("b", "x", "read", None) and policy.admits("b", "y", "read", None)
    assert policy.admits("c", "w", "write", None)
    assert policy.admits("d", "v", "write", {"ok": True})
    assert not policy.admits("d", "v", "read", {"ok": False})
    assert policy.uses("e") and not policy.uses("a")
