import pytest

from awm.gatewayclient import ids

UUID = "0E384A75-a06e-43fa-a19e-298e409a2730"


def test_r9_strips_dashes_and_lowers():
    assert ids.r9("cc", UUID) == "cc0e384a75a06e43faa19e298e409a2730"


def test_parse_round_trips_a_claude_uuid():
    assert ids.parse_r9(ids.r9("cc", UUID)) == ("cc", UUID.lower())


def test_parse_keeps_other_platforms_as_hex():
    assert ids.parse_r9("ds00ff") == ("ds", "00ff")


@pytest.mark.parametrize("bad", ["", "cc", "zz1234", "ccxyz", "b7312951"])
def test_parse_refuses_what_is_not_r9(bad):
    with pytest.raises(ids.BadSessionId):
        ids.parse_r9(bad)


def test_normalise_takes_a_bare_uuid_or_an_r9_id():
    want = "cc0e384a75a06e43faa19e298e409a2730"
    assert ids.normalise(UUID) == want
    assert ids.normalise(want.upper()) == want


def test_r9_refuses_an_unknown_platform():
    with pytest.raises(ids.BadSessionId):
        ids.r9("oc", UUID)


def test_any_length_of_hex_is_an_r9_id():
    assert ids.normalise("cc1234") == "cc1234"
    assert ids.normalise("ds" + "a" * 40) == "ds" + "a" * 40


def test_to_r9_answers_none_for_what_is_not_an_id():
    assert ids.to_r9("job00001") is None
    assert ids.to_r9(None) is None
    assert ids.to_r9(UUID) == "cc0e384a75a06e43faa19e298e409a2730"


def test_claude_uuid_only_for_a_full_claude_id():
    assert ids.claude_uuid(ids.r9("cc", UUID)) == UUID.lower()
    assert ids.claude_uuid("cc1234") is None
    assert ids.claude_uuid("ds" + "a" * 32) is None
