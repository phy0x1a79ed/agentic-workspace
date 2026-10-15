"""Unit tests for the landing page's tag/filter feature: ``selected_tags``
must never outlive the last ``page_tags`` row that justified it, so a fully
deleted tag stops appearing as a stale filter option."""

from __future__ import annotations

import pytest

from awm.httpsfront import store

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


class TestSelectedTagsPruning:
    def test_selected_tag_survives_while_in_use(self, awm_workspace):
        store.init()
        dao = store.LandingDAO()
        dao.add_tag("science", "physics")
        dao.select_tag("physics")
        assert dao.selected_tags() == ["physics"]

    def test_removing_last_use_prunes_selection(self, awm_workspace):
        store.init()
        dao = store.LandingDAO()
        dao.add_tag("science", "physics")
        dao.select_tag("physics")
        dao.remove_tag("science", "physics")
        assert dao.selected_tags() == []

    def test_removing_one_of_several_pages_keeps_selection(self, awm_workspace):
        store.init()
        dao = store.LandingDAO()
        dao.add_tag("science", "physics")
        dao.add_tag("math", "physics")
        dao.select_tag("physics")
        dao.remove_tag("science", "physics")
        assert dao.selected_tags() == ["physics"]
        dao.remove_tag("math", "physics")
        assert dao.selected_tags() == []

    def test_stale_selection_from_before_the_fix_is_filtered_on_read(self, awm_workspace):
        store.init()
        dao = store.LandingDAO()
        # Simulate a row that predates the remove_tag prune (e.g. from an
        # older DB) — selected_tags() must not surface it either.
        dao.execute("INSERT OR IGNORE INTO selected_tags (tag) VALUES (?)", ("ghost",))
        assert dao.selected_tags() == []
