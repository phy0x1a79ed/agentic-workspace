"""Tests for the discord_operators table CRUD."""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.unit, pytest.mark.smoke]

import pytest


@pytest.fixture()
def operators(awm_workspace):
    from awm.discord import operators as ops
    return ops


class TestAddOperator:
    def test_inserts_row(self, operators):
        entry = operators.add_operator("123456789", "tony")
        assert entry["discord_user_id"] == "123456789"
        assert entry["awm_user"] == "tony"
        assert entry["added_at"]

    def test_upsert_overrides_awm_user(self, operators):
        operators.add_operator("123", "alice")
        operators.add_operator("123", "bob")
        assert operators.lookup("123") == "bob"

    def test_rejects_blank_inputs(self, operators):
        with pytest.raises(ValueError):
            operators.add_operator("", "alice")
        with pytest.raises(ValueError):
            operators.add_operator("123", "")


class TestRemoveOperator:
    def test_returns_true_when_row_existed(self, operators):
        operators.add_operator("999", "ghost")
        assert operators.remove_operator("999") is True
        assert operators.lookup("999") is None

    def test_returns_false_when_no_row(self, operators):
        assert operators.remove_operator("nope") is False


class TestListLookup:
    def test_list_returns_all_rows(self, operators):
        operators.add_operator("1", "a")
        operators.add_operator("2", "b")
        rows = operators.list_operators()
        ids = {r["discord_user_id"] for r in rows}
        assert {"1", "2"} <= ids

    def test_lookup_returns_awm_user(self, operators):
        operators.add_operator("42", "operator")
        assert operators.lookup("42") == "operator"

    def test_lookup_missing_returns_none(self, operators):
        assert operators.lookup("absent") is None
        assert operators.lookup("") is None
