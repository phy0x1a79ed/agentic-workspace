"""Goal tests — the level union, the supersede collapse, and the invariants.

Goals ride the same append-only post log as journals. The three things that
must hold: journals are untouched, nothing is ever mutated or deleted, and the
in-force read neither ranks nor truncates.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.scopes]

USER = "user:tony"


class TestLevels:
    def test_level_maps_to_channel(self):
        from awm.scopes.goals import owner_for_level, level_for_owner
        assert owner_for_level("workspace") == ("", "workspace")
        assert owner_for_level("project", "awm") == ("", "project:awm")
        assert owner_for_level("scope", "awm", "svc-scopes") == ("awm", "svc-scopes")
        # and back
        assert level_for_owner("", "workspace") == "workspace"
        assert level_for_owner("", "project:awm") == "project"
        assert level_for_owner("awm", "svc-scopes") == "scope"

    def test_level_needs_its_arguments(self):
        from awm.scopes.goals import owner_for_level, GoalError
        with pytest.raises(GoalError):
            owner_for_level("project")
        with pytest.raises(GoalError):
            owner_for_level("scope", "awm")
        with pytest.raises(GoalError):
            owner_for_level("galaxy")

    def test_read_unions_three_levels_broad_to_specific(self, scopes_workspace):
        from awm.scopes import goals
        goals.set_goal(objective="ship things that work", author=USER,
                       level="workspace")
        goals.set_goal(objective="make ecspr work", author=USER,
                       level="project", project="fabfos")
        goals.set_goal(objective="a useful AAM by any means", author=USER,
                       level="scope", project="fabfos", scope="nosco")
        found = goals.read_goals("fabfos", "nosco")
        assert [g.level for g in found] == ["workspace", "project", "scope"]
        assert found[0].objective == "ship things that work"
        assert found[-1].objective == "a useful AAM by any means"

    def test_read_skips_levels_it_cannot_address(self, scopes_workspace):
        from awm.scopes import goals
        goals.set_goal(objective="ship things that work", author=USER,
                       level="workspace")
        goals.set_goal(objective="make ecspr work", author=USER,
                       level="project", project="fabfos")
        # No project → only the workspace frame is reachable.
        assert [g.level for g in goals.read_goals()] == ["workspace"]
        # Project but no scope → workspace + project.
        assert [g.level for g in goals.read_goals("fabfos")] == ["workspace", "project"]

    def test_levels_filter_narrows(self, scopes_workspace):
        from awm.scopes import goals
        goals.set_goal(objective="frame", author=USER, level="workspace")
        goals.set_goal(objective="campaign", author=USER, level="scope",
                       project="awm", scope="dev")
        only = goals.read_goals("awm", "dev", levels=["scope"])
        assert [g.objective for g in only] == ["campaign"]

    def test_a_scope_does_not_see_a_sibling_scopes_goal(self, scopes_workspace):
        from awm.scopes import goals
        goals.set_goal(objective="mine", author=USER, level="scope",
                       project="awm", scope="dev")
        assert goals.read_goals("awm", "other") == []


class TestSupersede:
    def test_revision_keeps_the_prior_record(self, scopes_workspace):
        from awm.scopes import goals, channel
        first = goals.set_goal(objective="map every reaction fully", author=USER,
                               level="scope", project="fabfos", scope="nosco")
        second = goals.set_goal(objective="a useful AAM, partial if need be",
                                author=USER, supersedes=first.id)
        # The current read returns only the revision …
        current = goals.read_goals("fabfos", "nosco")
        assert [g.id for g in current] == [second.id]
        # … but nothing was deleted: the raw log still holds both.
        raw = channel.fetch(project="fabfos", scope="nosco", kind="goal")
        assert {p.id for p in raw} == {first.id, second.id}
        assert goals.get_goal(first.id) is not None

    def test_revision_inherits_the_channel(self, scopes_workspace):
        from awm.scopes import goals
        first = goals.set_goal(objective="v1", author=USER, level="project",
                               project="fabfos")
        second = goals.set_goal(objective="v2", author=USER, supersedes=first.id)
        assert (second.project, second.scope) == (first.project, first.scope)
        assert second.level == "project"

    def test_revision_may_be_moved_to_another_level(self, scopes_workspace):
        """An explicit level moves a goal; the collapse is computed over the
        whole union, so the record it supersedes still drops out."""
        from awm.scopes import goals
        first = goals.set_goal(objective="v1", author=USER, level="scope",
                               project="awm", scope="dev")
        second = goals.set_goal(objective="v2", author=USER, level="workspace",
                                supersedes=first.id)
        found = goals.read_goals("awm", "dev")
        assert [g.id for g in found] == [second.id]
        assert second.level == "workspace"

    def test_revision_inherits_unstated_disposition_fields(self, scopes_workspace):
        """Restating only the objective must not drop the stop line out of the
        in-force set — that is the same silent loss this feature targets, by a
        different route."""
        from awm.scopes import goals
        first = goals.set_goal(
            objective="obtain a useful AAM", author=USER, level="scope",
            project="fabfos", scope="nosco",
            fallback="map parts of reactions", stop_line="wrong chemistry stops",
            noise="coverage caveats")
        second = goals.set_goal(objective="obtain a useful AAM, faster",
                                author=USER, supersedes=first.id)
        assert second.fallback == "map parts of reactions"
        assert second.stop_line == "wrong chemistry stops"
        assert second.noise == "coverage caveats"
        assert second.missing == []

    def test_revision_can_override_one_field_and_keep_the_rest(self, scopes_workspace):
        from awm.scopes import goals
        first = goals.set_goal(
            objective="v1", author=USER, level="workspace",
            fallback="old ladder", stop_line="old stop", noise="old noise")
        second = goals.set_goal(objective="v2", author=USER,
                                supersedes=first.id, fallback="new ladder")
        assert second.fallback == "new ladder"
        assert second.stop_line == "old stop"

    def test_an_explicit_empty_string_clears_a_field(self, scopes_workspace):
        """None means carry forward; "" means the user retracted it."""
        from awm.scopes import goals
        first = goals.set_goal(objective="v1", author=USER, level="workspace",
                               noise="stop mentioning coverage")
        second = goals.set_goal(objective="v2", author=USER,
                                supersedes=first.id, noise="")
        assert second.noise == ""
        assert "noise" in second.missing

    def test_a_fresh_goal_does_not_inherit_from_anywhere(self, scopes_workspace):
        from awm.scopes import goals
        goals.set_goal(objective="v1", author=USER, level="workspace",
                       stop_line="a stop")
        fresh = goals.set_goal(objective="unrelated", author=USER,
                               level="workspace")
        assert fresh.stop_line == ""

    def test_chain_of_three_collapses_to_the_last(self, scopes_workspace):
        from awm.scopes import goals
        a = goals.set_goal(objective="v1", author=USER, level="workspace")
        b = goals.set_goal(objective="v2", author=USER, supersedes=a.id)
        c = goals.set_goal(objective="v3", author=USER, supersedes=b.id)
        assert [g.id for g in goals.read_goals()] == [c.id]

    def test_supersede_unknown_id_refuses(self, scopes_workspace):
        from awm.scopes import goals
        with pytest.raises(goals.GoalError):
            goals.set_goal(objective="v2", author=USER, supersedes="not-a-goal")

    def test_set_needs_a_level_or_a_supersede(self, scopes_workspace):
        from awm.scopes import goals
        with pytest.raises(goals.GoalError):
            goals.set_goal(objective="floating", author=USER)

    def test_empty_objective_refuses(self, scopes_workspace):
        from awm.scopes import goals
        with pytest.raises(goals.GoalError):
            goals.set_goal(objective="   ", author=USER, level="workspace")


class TestRetire:
    def test_retire_drops_it_from_the_current_read(self, scopes_workspace):
        from awm.scopes import goals
        g = goals.set_goal(objective="done with this", author=USER,
                           level="scope", project="awm", scope="dev")
        goals.retire_goal(g.id, author=USER, reason="shipped")
        assert goals.read_goals("awm", "dev") == []
        # Still readable — a tombstone is not a delete.
        assert goals.get_goal(g.id) is not None

    def test_tombstone_never_appears_as_a_goal(self, scopes_workspace):
        from awm.scopes import goals
        a = goals.set_goal(objective="keep", author=USER, level="workspace")
        b = goals.set_goal(objective="drop", author=USER, level="workspace")
        goals.retire_goal(b.id, author=USER)
        found = goals.read_goals()
        assert [g.id for g in found] == [a.id]

    def test_double_retire_refuses(self, scopes_workspace):
        from awm.scopes import goals
        g = goals.set_goal(objective="x", author=USER, level="workspace")
        t = goals.retire_goal(g.id, author=USER)
        with pytest.raises(goals.GoalError):
            goals.retire_goal(t.id, author=USER)

    def test_retire_unknown_id_refuses(self, scopes_workspace):
        from awm.scopes import goals
        with pytest.raises(goals.GoalError):
            goals.retire_goal("nope", author=USER)


class TestHistory:
    def test_chain_is_retrievable_from_any_link(self, scopes_workspace):
        from awm.scopes import goals
        a = goals.set_goal(objective="v1", author=USER, level="workspace")
        b = goals.set_goal(objective="v2", author=USER, supersedes=a.id)
        c = goals.set_goal(objective="v3", author=USER, supersedes=b.id)
        for probe in (a.id, b.id, c.id):
            assert [g.id for g in goals.history(probe)] == [a.id, b.id, c.id]

    def test_history_includes_the_tombstone(self, scopes_workspace):
        from awm.scopes import goals
        a = goals.set_goal(objective="v1", author=USER, level="workspace")
        t = goals.retire_goal(a.id, author=USER, reason="obsolete")
        chain = goals.history(a.id)
        assert [g.id for g in chain] == [a.id, t.id]
        assert chain[-1].is_tombstone

    def test_history_excludes_an_unrelated_goal(self, scopes_workspace):
        from awm.scopes import goals
        a = goals.set_goal(objective="v1", author=USER, level="workspace")
        goals.set_goal(objective="unrelated", author=USER, level="workspace")
        assert [g.id for g in goals.history(a.id)] == [a.id]


class TestDisposition:
    def test_all_four_fields_round_trip(self, scopes_workspace):
        from awm.scopes import goals
        goals.set_goal(
            objective="obtain a useful AAM", author=USER, level="scope",
            project="fabfos", scope="nosco",
            fallback="map parts of reactions when they cannot be mapped fully",
            stop_line="wrong chemistry stops; unmapped is incomplete, say so",
            noise="coverage caveats that do not change the next step",
        )
        g = goals.read_goals("fabfos", "nosco")[0]
        assert g.objective == "obtain a useful AAM"
        assert "map parts" in g.fallback
        assert "wrong chemistry" in g.stop_line
        assert "coverage caveats" in g.noise
        assert g.missing == []
        _, text = goals.read_rendered("fabfos", "nosco")
        for value in (g.fallback, g.stop_line, g.noise):
            assert value in text, "every disposition field must reach the render"

    def test_a_partial_goal_is_recorded_and_reports_what_is_missing(self, scopes_workspace):
        """Refusing to record until all four fields are stated would be the same
        all-or-nothing reflex this whole feature exists to correct."""
        from awm.scopes import goals
        g = goals.set_goal(objective="make ecspr work", author=USER,
                           level="project", project="fabfos",
                           stop_line="a wrong answer stops")
        assert g.missing == ["fallback", "noise"]
        assert goals.read_goals("fabfos")[0].objective == "make ecspr work"


class TestRender:
    def test_render_carries_the_comparison_instruction(self, scopes_workspace):
        """AC6: the instruction rides with the data, because the skill that
        wrote the goal is out of context by the time it matters."""
        from awm.scopes import goals
        goals.set_goal(objective="make ecspr work", author=USER,
                       level="project", project="fabfos")
        _, text = goals.read_rendered("fabfos", "nosco")
        assert "make ecspr work" in text
        assert "A diagnosis is not a deliverable" in text
        assert "Is this at the altitude that was asked" in text
        assert "Partial and honestly labelled beats refusal" in text

    def test_empty_render_still_says_what_to_do(self, scopes_workspace):
        from awm.scopes import goals
        _, text = goals.read_rendered("awm", "dev")
        assert "Nothing recorded yet" in text

    def test_render_orders_levels_broad_to_specific(self, scopes_workspace):
        from awm.scopes import goals
        goals.set_goal(objective="THE-FRAME", author=USER, level="workspace")
        goals.set_goal(objective="THE-PROJECT", author=USER, level="project",
                       project="fabfos")
        goals.set_goal(objective="THE-CAMPAIGN", author=USER, level="scope",
                       project="fabfos", scope="nosco")
        _, text = goals.read_rendered("fabfos", "nosco")
        assert (text.index("THE-FRAME") < text.index("THE-PROJECT")
                < text.index("THE-CAMPAIGN"))

    def test_render_flags_missing_fields(self, scopes_workspace):
        from awm.scopes import goals
        goals.set_goal(objective="partial", author=USER, level="workspace")
        _, text = goals.read_rendered()
        assert "not yet stated" in text


class TestJournalsUnaffected:
    """Goals must be invisible to everything journals drive."""

    def test_goals_do_not_appear_in_history_md(self, scopes_workspace):
        from awm.scopes import channel, goals
        from awm.scopes.scopes import _generate_history_md
        channel.post("awm", "dev", author="agent:awm/dev", kind="journal",
                     meta={"title": "A real session"}, body="did the thing")
        before = _generate_history_md("awm", "dev")
        goals.set_goal(objective="GOAL-SHOULD-NOT-RENDER", author=USER,
                       level="scope", project="awm", scope="dev")
        after = _generate_history_md("awm", "dev")
        assert after == before
        assert "GOAL-SHOULD-NOT-RENDER" not in after

    def test_a_goal_post_does_not_trigger_a_refresh(self, scopes_workspace, monkeypatch):
        """Only kind='journal' auto-refreshes history.md."""
        from awm.scopes.operations import scope_channel
        from awm.scopes import scopes as scopes_mod
        calls = []
        monkeypatch.setattr(scopes_mod, "awm_refresh",
                            lambda *a, **k: calls.append(a))
        scope_channel._handle_scope_post({
            "project": "awm", "scope": "dev", "author": USER,
            "kind": "goal", "body": "a goal by the raw post path",
        })
        assert calls == []

    def test_goal_kind_filter_excludes_journals(self, scopes_workspace):
        from awm.scopes import channel, goals
        channel.post("awm", "dev", author="agent:awm/dev", kind="journal",
                     body="a journal", meta={"title": "j"})
        goals.set_goal(objective="a goal", author=USER, level="scope",
                       project="awm", scope="dev")
        found = goals.read_goals("awm", "dev")
        assert [g.objective for g in found] == ["a goal"]


class TestNoTruncation:
    def test_read_returns_everything_in_force(self, scopes_workspace):
        """A stop line rotated out of a top-k is a correctness failure, not a
        relevance tradeoff. scope_fetch defaults to limit=50; this path has no
        limit at all."""
        from awm.scopes import goals
        for i in range(120):
            goals.set_goal(objective=f"goal {i}", author=USER, level="workspace")
        assert len(goals.read_goals()) == 120


class TestOperations:
    def test_verbs_land_on_the_scope_domain(self):
        """The MCP projection splits a tool name on its first underscore, so
        these must be scope_goal_* to reach the model as scope(verb='goal_*')
        rather than minting a 'goal' domain."""
        from awm.scopes.operations.goals import (
            GOAL_MANIFEST_FUNCTIONS, GOAL_HANDLERS,
        )
        names = {f["name"] for f in GOAL_MANIFEST_FUNCTIONS}
        assert names == {"scope_goal_set", "scope_goal_retire",
                         "scope_goal_read", "scope_goal_history"}
        assert names == set(GOAL_HANDLERS)
        for n in names:
            assert n.partition("_")[0] == "scope"

    def test_objective_is_the_terminal_param(self):
        """Same reason as scope_post's body: a long free-text value can bleed
        past its closing tag and swallow any parameter serialized after it."""
        from awm.scopes.operations.goals import GOAL_MANIFEST_FUNCTIONS
        fn = next(f for f in GOAL_MANIFEST_FUNCTIONS
                  if f["name"] == "scope_goal_set")
        names = [p["name"] for p in fn["params"]]
        assert names[-1] == "objective", f"got order {names}"
        for p in ("level", "supersedes", "fallback", "stop_line", "noise"):
            assert names.index(p) < names.index("objective")

    def test_verbs_are_registered_in_the_manifest(self):
        from awm.scopes.hub_adapter import API_MANIFEST, HANDLERS
        declared = {f["name"] for f in API_MANIFEST["functions"]}
        for n in ("scope_goal_set", "scope_goal_retire", "scope_goal_read",
                  "scope_goal_history"):
            assert n in declared
            assert n in HANDLERS

    def test_no_delete_verb_exists(self):
        """Editing is supersede. Nothing in this surface mutates or deletes."""
        from awm.scopes.hub_adapter import HANDLERS
        assert not any("goal" in n and "delete" in n for n in HANDLERS)

    def test_handlers_round_trip(self, scopes_workspace):
        from awm.scopes.operations.goals import GOAL_HANDLERS
        created = GOAL_HANDLERS["scope_goal_set"]({
            "author": USER, "level": "scope", "project": "fabfos",
            "scope": "nosco", "fallback": "map parts",
            "objective": "obtain a useful AAM",
        })["goal"]
        revised = GOAL_HANDLERS["scope_goal_set"]({
            "author": USER, "supersedes": created["id"],
            "objective": "obtain a useful AAM, partial rows allowed",
        })["goal"]

        read = GOAL_HANDLERS["scope_goal_read"]({
            "project": "fabfos", "scope": "nosco"})
        assert read["total"] == 1
        assert read["goals"][0]["id"] == revised["id"]
        assert "A diagnosis is not a deliverable" in read["rendered"]

        chain = GOAL_HANDLERS["scope_goal_history"]({"goal_id": created["id"]})
        assert [g["id"] for g in chain["chain"]] == [created["id"], revised["id"]]

        GOAL_HANDLERS["scope_goal_retire"]({
            "goal_id": revised["id"], "author": USER})
        assert GOAL_HANDLERS["scope_goal_read"](
            {"project": "fabfos", "scope": "nosco"})["total"] == 0

    def test_levels_accepts_a_comma_string(self, scopes_workspace):
        """A harness that flattens an array param must not silently read zero
        levels and report 'no goals'."""
        from awm.scopes.operations.goals import GOAL_HANDLERS
        GOAL_HANDLERS["scope_goal_set"]({
            "author": USER, "level": "workspace", "objective": "the frame"})
        out = GOAL_HANDLERS["scope_goal_read"]({"levels": "workspace"})
        assert out["total"] == 1
