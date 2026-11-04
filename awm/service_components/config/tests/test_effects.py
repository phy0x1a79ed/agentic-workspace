"""A verb's effect: read, queue, write or secret, with write as the default."""

import pytest

import awm.config as config


def test_verb_effect():
    assert config.EFFECTS == ("read", "queue", "write", "secret")
    assert config.verb_effect({"name": "x"}) == "write"
    assert config.verb_effect({"name": "x", "effect": None}) == "write"
    assert config.verb_effect({"name": "x", "effect": "read"}) == "read"
    with pytest.raises(ValueError):
        config.verb_effect({"name": "x", "effect": "delete"})
