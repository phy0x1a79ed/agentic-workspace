import pytest

from awm.rlm_factorio import validate


def test_numeric_strings_are_converted():
    out = validate.check("move", {"x": ".5", "y": "-12"})
    assert out == {"x": 0.5, "y": -12.0}


@pytest.mark.parametrize("args", [
    {"x": "abc", "y": 1},
    {"x": float("nan"), "y": 1},
    {"x": True, "y": 1},
    {"x": 2e6, "y": 1},
])
def test_bad_coordinates_refused(args):
    with pytest.raises(validate.ArgError):
        validate.check("move", args)


def test_radius_bounds():
    with pytest.raises(validate.ArgError, match="radius"):
        validate.check("observe", {"radius": 500})
    assert validate.check("observe", {"radius": "32"})["radius"] == 32


def test_names_must_be_strings():
    with pytest.raises(validate.ArgError, match="name"):
        validate.check("build", {"name": 7, "x": 0, "y": 0})
    with pytest.raises(validate.ArgError):
        validate.check("craft", {"recipe": "iron-gear-wheel'); game.print('x"})


def test_oversized_screenshot_refused():
    with pytest.raises(validate.ArgError, match="width"):
        validate.check("screenshot", {"width": 100_000})


def test_scan_names_accept_one_or_many():
    assert validate.check("scan", {"name": "stone-furnace"})["name"] == ["stone-furnace"]
    assert validate.check("scan", {"type": ["inserter", "assembling-machine"]})["type"] == [
        "inserter", "assembling-machine"]


def test_directions_are_16_way():
    assert validate.check("build", {"direction": "northeast", "name": "x", "x": 0, "y": 0})
    with pytest.raises(validate.ArgError):
        validate.check("build", {"direction": "up", "name": "x", "x": 0, "y": 0})


def test_unknown_verb_passes_through():
    assert validate.check("status", {"anything": object}) == {"anything": object}


def test_order_verbs_are_bounded():
    assert validate.check("walk", {"x": "1", "y": 2, "guard": "false"})["guard"] is False
    with pytest.raises(validate.ArgError):
        validate.check("walk", {"x": 1, "y": 2, "guard_radius": 100})
    with pytest.raises(validate.ArgError, match="cond"):
        validate.check("wait", {"cond": "forever"})
    assert validate.check("wait", {"cond": "status", "status": "working"})["status"] == ["working"]
    with pytest.raises(validate.ArgError):
        validate.check("throw", {"item": "defender-capsule", "count": 500})
    with pytest.raises(validate.ArgError):
        validate.check("order", {"order": 1, "timeout": 3600})
