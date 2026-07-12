"""Regression tests for double-encoded ``meta`` resilience.

A journal post's ``meta`` is stored as ``json.dumps(meta)``. A harness that
hands ``meta`` in as a JSON *string* (rather than an object) double-encodes it,
so on read ``json.loads`` yields a ``str`` and ``meta.get(...)`` blows up with
``'str' object has no attribute 'get'`` — which crashed ``scope_refresh`` for
the whole project (one bad row poisons ``_generate_history_md``). These cover
both the write-side normalization and the read-side coercion that fix it.
"""
from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.scopes]


class TestCoerceMeta:
    def test_dict_passthrough(self):
        from awm.scopes.channel import _coerce_meta
        assert _coerce_meta({"a": 1}) == {"a": 1}

    def test_none_and_empty(self):
        from awm.scopes.channel import _coerce_meta
        assert _coerce_meta(None) == {}
        assert _coerce_meta("") == {}

    def test_json_object_string(self):
        from awm.scopes.channel import _coerce_meta
        assert _coerce_meta('{"title": "x"}') == {"title": "x"}

    def test_double_encoded_recovers(self):
        """The poisoned-row case: a JSON string that itself holds JSON."""
        from awm.scopes.channel import _coerce_meta
        inner = {"title": "Project setup", "outcome": "success"}
        double = json.dumps(json.dumps(inner))  # json.dumps of a JSON string
        assert _coerce_meta(double) == inner

    def test_scalar_becomes_empty(self):
        from awm.scopes.channel import _coerce_meta
        assert _coerce_meta(json.dumps(42)) == {}
        assert _coerce_meta(json.dumps("plain text")) == {}

    def test_garbage_becomes_empty(self):
        from awm.scopes.channel import _coerce_meta
        assert _coerce_meta("not json at all {") == {}


class TestWriteSideNormalization:
    def test_str_meta_stored_as_dict(self, scopes_workspace):
        """A harness that posts meta as a JSON string round-trips to a dict."""
        from awm.scopes import channel
        p = channel.post("awm", "dev", author="agent:awm/dev", body="debrief",
                         kind="journal", meta='{"title": "Did it", "outcome": "success"}')
        assert isinstance(p.meta, dict)
        assert p.meta["outcome"] == "success"
        got = channel.get_post(p.id)
        assert got.meta == {"title": "Did it", "outcome": "success"}

    def test_dict_meta_unchanged(self, scopes_workspace):
        from awm.scopes import channel
        p = channel.post("awm", "dev", author="agent:awm/dev", body="b",
                         kind="journal", meta={"title": "T"})
        assert channel.get_post(p.id).meta == {"title": "T"}

    def test_none_meta(self, scopes_workspace):
        from awm.scopes import channel
        p = channel.post("awm", "dev", author="user:a", body="hi", kind="message")
        assert p.meta == {}


class TestReadSideResilience:
    def _insert_double_encoded(self, conn, project, scope):
        from awm.scopes.identity import now_ms
        inner = {"title": "Project setup with dev and factorio scopes",
                 "outcome": "success", "skill_path": "awm/debrief.md"}
        conn.execute(
            "INSERT INTO scope_posts "
            "(id, owner_project, owner_scope, author, kind, body, meta, ts) "
            "VALUES (?,?,?,?, 'journal', ?, ?, ?)",
            ("poisoned", project, scope, f"agent:{project}/{scope}",
             "did setup", json.dumps(json.dumps(inner)), now_ms()),
        )
        conn.commit()
        return inner

    def test_row_to_post_recovers(self, scopes_workspace, scopes_dao_conn):
        from awm.scopes import channel
        inner = self._insert_double_encoded(scopes_dao_conn, "awm", "dev")
        got = channel.get_post("poisoned")
        # pydantic ScopePost.meta is dict — a str would have raised here.
        assert got.meta == inner

    def test_refresh_history_survives_poisoned_row(
            self, scopes_workspace, seeded_scopes, scopes_dao_conn):
        from awm.scopes import scopes
        self._insert_double_encoded(scopes_dao_conn, "proj-a", "scope-1")
        # Before the fix this raised 'str' object has no attribute 'get'.
        content = scopes.refresh_history("proj-a", "scope-1")
        assert "Project setup with dev and factorio scopes" in content
