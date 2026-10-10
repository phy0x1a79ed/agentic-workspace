"""Operation.effect / category declaration and validation."""

from __future__ import annotations

import pytest

from awm.gateway.operations import JsonOutput, Operation

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def _op(**kw) -> Operation:
    return Operation(
        name="t", description="", service_func=lambda: None,
        http_method="GET", http_path="/t", cli_group="t", cli_command="t",
        output=JsonOutput(), **kw,
    )


def test_defaults():
    op = _op()
    assert op.effect == "write"
    assert op.category is None


@pytest.mark.parametrize("effect", ["read", "queue", "write", "secret"])
def test_valid_effects(effect):
    assert _op(effect=effect, category="kb").category == "kb"


def test_invalid_effect_rejected():
    with pytest.raises(ValueError, match="effect"):
        _op(effect="delete")


def test_effects_single_definition():
    from awm.config import EFFECTS
    import awm.gateway.operations as ops
    assert ops.EFFECTS is EFFECTS
