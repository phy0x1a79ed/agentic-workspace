"""Tests for the platform-scoped social_operators table CRUD."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture()
def operators(awm_workspace):
    from awm.social import operators as ops
    return ops


class TestAddOperator:
    def test_inserts_row(self, operators):
        entry = operators.add_operator("discord", "123456789", "tony")
        assert entry["platform"] == "discord"
        assert entry["platform_user_id"] == "123456789"
        assert entry["awm_user"] == "tony"
        assert entry["added_at"]

    def test_upsert_overrides_awm_user(self, operators):
        operators.add_operator("slack", "U123", "alice")
        operators.add_operator("slack", "U123", "bob")
        assert operators.lookup("slack", "U123") == "bob"

    def test_same_id_different_platform_is_distinct(self, operators):
        operators.add_operator("discord", "X1", "d_user")
        operators.add_operator("slack", "X1", "s_user")
        assert operators.lookup("discord", "X1") == "d_user"
        assert operators.lookup("slack", "X1") == "s_user"

    def test_rejects_blank_inputs(self, operators):
        with pytest.raises(ValueError):
            operators.add_operator("", "U1", "alice")
        with pytest.raises(ValueError):
            operators.add_operator("slack", "", "alice")
        with pytest.raises(ValueError):
            operators.add_operator("slack", "U1", "")


class TestRemoveOperator:
    def test_returns_true_when_row_existed(self, operators):
        operators.add_operator("discord", "999", "ghost")
        assert operators.remove_operator("discord", "999") is True
        assert operators.lookup("discord", "999") is None

    def test_returns_false_when_no_row(self, operators):
        assert operators.remove_operator("discord", "nope") is False


class TestListLookup:
    def test_list_all_and_by_platform(self, operators):
        operators.add_operator("discord", "1", "a")
        operators.add_operator("slack", "2", "b")
        assert len(operators.list_operators()) == 2
        d = operators.list_operators("discord")
        assert len(d) == 1 and d[0]["platform_user_id"] == "1"

    def test_lookup_missing_returns_none(self, operators):
        assert operators.lookup("discord", "absent") is None
        assert operators.lookup("", "") is None
