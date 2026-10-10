import sqlite3

import pytest

from awm.board.parties import NotFound, Parties, can_see


def card(sender="tony", recipient="collins"):
    return {"sender": {"swarm": sender, "principal": "x", "party": "p"}, "recipient": recipient}


def test_add_resolves_and_returns_the_token_once(parties):
    row, token = parties.add("tony", "agent", "domestic")
    assert row["swarm"] == "tony" and row["principal"] == "agent"
    assert row["relation"] == "domestic" and row["revoked"] is False
    assert parties.resolve(token) == row
    assert parties.list() == [row]


def test_only_the_hash_is_stored(parties, tmp_path):
    row, token = parties.add("tony", "agent", "domestic")
    assert token not in row.values()
    raw = sqlite3.connect(tmp_path / "parties.db")
    dump = "\n".join(raw.iterdump())
    assert token not in dump
    assert row["token_hash"] in dump


def test_unknown_and_empty_tokens_resolve_to_none(parties):
    parties.add("tony", "agent", "domestic")
    assert parties.resolve("nope") is None
    assert parties.resolve("") is None
    assert parties.resolve(None) is None


def test_revocation_takes_effect_on_the_next_resolve(parties):
    row, token = parties.add("collins", "john", "sovereign")
    assert parties.resolve(token)
    revoked = parties.revoke(row["party_id"])
    assert revoked["revoked"] is True
    assert parties.resolve(token) is None
    assert parties.list()[0]["revoked"] is True


def test_revoking_an_unknown_party_is_not_found(parties):
    with pytest.raises(NotFound):
        parties.revoke("missing")


@pytest.mark.parametrize("swarm,principal,relation", [
    ("Tony", "a", "domestic"), ("open", "a", "domestic"), ("", "a", "domestic"),
    ("tony", "A B", "domestic"), ("tony", "a", "friendly")])
def test_bad_vocabulary_is_refused(parties, swarm, principal, relation):
    with pytest.raises(ValueError):
        parties.add(swarm, principal, relation)


def test_tokens_survive_a_restart(tmp_path):
    first = Parties(tmp_path / "p.db")
    _, token = first.add("tony", "agent", "domestic")
    assert Parties(tmp_path / "p.db").resolve(token)["swarm"] == "tony"


def test_two_parties_never_share_a_token(parties):
    _, a = parties.add("tony", "agent", "domestic")
    _, b = parties.add("tony", "agent", "domestic")
    assert a != b


def test_visibility_rule():
    tony = {"swarm": "tony"}
    assert can_see(tony, card(sender="tony", recipient="collins"))   # sent
    assert can_see(tony, card(sender="collins", recipient="tony"))   # addressed to it
    assert can_see(tony, card(sender="collins", recipient="open"))   # open
    assert not can_see(tony, card(sender="collins", recipient="mock"))
