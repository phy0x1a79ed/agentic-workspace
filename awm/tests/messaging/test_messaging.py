"""Tests for the messaging service."""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.messaging, pytest.mark.slow]

import threading

import pytest

from awm.models import MessageSendRequest
from awm.services import messaging


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def _init_messaging(awm_workspace, seeded_scopes):
    """Ensure DB and scopes are ready for messaging tests."""
    return awm_workspace


@pytest.fixture()
def seeded_messages(_init_messaging, db_conn):
    """Insert sample messages for search/filter tests on the v37 shape.

    The fixture preserves the legacy (scope, sender, …) tuple shape so test
    code keeps unpacking it the same way; the row inserted into v37
    ``messages`` uses recipient_id/sender_id resolved via
    services.messaging._resolve_recipient + _resolve_sender, then ISO
    timestamps converted to INTEGER ms.
    """
    from awm.services import identity, messaging

    now_ms = identity.now_ms()
    # (scope, sender, msg_type, subject, body, metadata, status, ts, read_ts)
    rows = [
        ("workspace", "agent-1", "status_update", "Build complete",
         "All tests passed", None, "unread", now_ms, None),
        ("project:proj-a", "agent-2", "notification", "Review needed",
         "PR #42 ready", None, "unread", now_ms, None),
        ("scope:proj-a/scope-1", "workspace", "scope_assignment",
         "Implement feature X", "See the spec below",
         '{"priority": "high"}', "unread", now_ms, None),
        ("scope:proj-a/scope-1", "workspace", "plan", "Execution plan",
         "Step 1: do the thing", None, "read", now_ms, now_ms),
    ]
    for scope, sender, msg_type, subject, body, metadata, status, ts, read_ts in rows:
        recipient_id, _ = messaging._resolve_recipient(scope, conn=db_conn)
        sender_id = messaging._resolve_sender(sender, conn=db_conn)
        db_conn.execute(
            "INSERT INTO messages "
            "(recipient_id, sender_id, msg_type, subject, body, metadata, "
            " status, created_at, read_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (recipient_id, sender_id, msg_type, subject, body, metadata,
             status, ts, read_ts),
        )
    db_conn.commit()
    # Return the legacy ISO-shape tuple for callers that depend on it.
    iso = identity.ms_to_iso(now_ms)
    return [(*r[:7], iso, iso if r[8] else None) for r in rows]


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------

class TestSendMessage:
    def test_send_valid(self, _init_messaging):
        req = MessageSendRequest(
            scope="workspace",
            sender="test-agent",
            msg_type="notification",
            subject="Hello",
            body="World",
        )
        resp = messaging.send_message(req)
        assert "sent" in resp.message.lower()
        assert resp.msg is not None
        assert resp.msg.scope == "workspace"
        assert resp.msg.status == "unread"

    def test_send_to_project_scope(self, _init_messaging):
        req = MessageSendRequest(
            scope="project:proj-a",
            sender="workspace",
            msg_type="status_update",
            subject="Update",
            body="All good",
        )
        resp = messaging.send_message(req)
        assert resp.msg.scope == "project:proj-a"

    def test_send_to_scope(self, _init_messaging):
        req = MessageSendRequest(
            scope="scope:proj-a/scope-1",
            sender="workspace",
            msg_type="plan",
            subject="Plan",
            body="Do this",
        )
        resp = messaging.send_message(req)
        assert resp.msg.scope == "scope:proj-a/scope-1"

    def test_send_invalid_scope(self, _init_messaging):
        req = MessageSendRequest(
            scope="invalid",
            sender="test",
            msg_type="notification",
            subject="x",
            body="y",
        )
        with pytest.raises(ValueError, match="Invalid scope"):
            messaging.send_message(req)

    def test_send_all_msg_types(self, _init_messaging):
        for msg_type in ["scope_assignment", "reflection", "status_update", "notification", "plan"]:
            req = MessageSendRequest(
                scope="workspace",
                sender="test",
                msg_type=msg_type,
                subject=f"Test {msg_type}",
                body="body",
            )
            resp = messaging.send_message(req)
            assert resp.msg.msg_type == msg_type

    def test_send_with_metadata(self, _init_messaging):
        req = MessageSendRequest(
            scope="workspace",
            sender="test",
            msg_type="notification",
            subject="Meta test",
            body="body",
            metadata='{"key": "value"}',
        )
        resp = messaging.send_message(req)
        assert resp.msg.metadata == '{"key": "value"}'


# ---------------------------------------------------------------------------
# search_messages
# ---------------------------------------------------------------------------

class TestSearchMessages:
    def test_search_all(self, seeded_messages):
        resp = messaging.search_messages()
        assert resp.total == 4

    def test_search_by_scope(self, seeded_messages):
        resp = messaging.search_messages(scope="workspace")
        assert resp.total == 1
        assert resp.messages[0].scope == "workspace"

    def test_search_by_status(self, seeded_messages):
        resp = messaging.search_messages(status="read")
        assert resp.total == 1
        assert resp.messages[0].status == "read"

    def test_search_by_msg_type(self, seeded_messages):
        resp = messaging.search_messages(msg_type="scope_assignment")
        assert resp.total == 1

    def test_search_by_query(self, seeded_messages):
        resp = messaging.search_messages(query="tests passed")
        assert resp.total == 1
        assert resp.messages[0].subject == "Build complete"

    def test_search_returns_previews_without_body(self, seeded_messages):
        resp = messaging.search_messages()
        assert resp.total == 4
        for m in resp.messages:
            assert "body" not in m.model_dump()
            assert "metadata" not in m.model_dump()

    def test_search_query_in_subject(self, seeded_messages):
        resp = messaging.search_messages(query="Review needed")
        assert resp.total == 1

    def test_search_with_limit(self, seeded_messages):
        resp = messaging.search_messages(limit=2)
        assert resp.total == 2

    def test_search_combined_filters(self, seeded_messages):
        resp = messaging.search_messages(scope="scope:proj-a/scope-1", status="unread")
        assert resp.total == 1
        assert resp.messages[0].msg_type == "scope_assignment"

    def test_search_invalid_scope(self, seeded_messages):
        with pytest.raises(ValueError, match="Invalid scope"):
            messaging.search_messages(scope="bad scope")


# ---------------------------------------------------------------------------
# fetch_messages
# ---------------------------------------------------------------------------

class TestFetchMessages:
    def test_fetch_requires_valid_scope(self, seeded_messages):
        with pytest.raises(ValueError, match="Invalid scope"):
            messaging.fetch_messages(scope="bogus")

    def test_fetch_returns_full_bodies(self, seeded_messages):
        resp = messaging.fetch_messages(scope="scope:proj-a/scope-1")
        assert resp.total == 2
        for m in resp.messages:
            assert m.body is not None
            assert "body" in m.model_dump()
        subjects = {m.subject for m in resp.messages}
        assert subjects == {"Implement feature X", "Execution plan"}

    def test_fetch_does_not_mark_read_by_default(self, seeded_messages, db_conn):
        from awm.services import messaging as _msg
        resp = messaging.fetch_messages(scope="scope:proj-a/scope-1")
        assert resp.marked_read_count == 0
        recipient_id, _ = _msg._resolve_recipient(
            "scope:proj-a/scope-1", conn=db_conn,
        )
        row = db_conn.execute(
            "SELECT status FROM messages WHERE recipient_id=? AND subject=?",
            (recipient_id, "Implement feature X"),
        ).fetchone()
        assert row["status"] == "unread"

    def test_fetch_mark_read_flips_unread_only(self, seeded_messages, db_conn):
        resp = messaging.fetch_messages(scope="scope:proj-a/scope-1", mark_read=True)
        assert resp.total == 2
        assert resp.marked_read_count == 1
        for m in resp.messages:
            assert m.status == "read"
            assert m.read_at is not None
        again = messaging.fetch_messages(scope="scope:proj-a/scope-1", status="unread", mark_read=True)
        assert again.total == 0
        assert again.marked_read_count == 0

    def test_fetch_filter_by_status(self, seeded_messages):
        resp = messaging.fetch_messages(scope="scope:proj-a/scope-1", status="read")
        assert resp.total == 1
        assert resp.messages[0].subject == "Execution plan"

    def test_fetch_filter_by_msg_type(self, seeded_messages):
        resp = messaging.fetch_messages(scope="scope:proj-a/scope-1", msg_type="plan")
        assert resp.total == 1
        assert resp.messages[0].msg_type == "plan"

    def test_fetch_limit(self, seeded_messages):
        resp = messaging.fetch_messages(scope="scope:proj-a/scope-1", limit=1)
        assert resp.total == 1

    def test_fetch_empty(self, seeded_messages):
        resp = messaging.fetch_messages(scope="project:proj-b")
        assert resp.total == 0


# ---------------------------------------------------------------------------
# list_recipients
# ---------------------------------------------------------------------------

class TestListRecipients:
    def test_recipients_include_workspace(self, _init_messaging):
        recipients = messaging.list_recipients("*")
        assert "workspace" in recipients

    def test_recipients_include_active_projects(self, _init_messaging):
        recipients = messaging.list_recipients("*")
        assert "project:proj-a" in recipients

    def test_recipients_include_active_scopes(self, _init_messaging):
        recipients = messaging.list_recipients("*")
        assert "scope:proj-a/scope-1" in recipients

    def test_recipients_exclude_completed_scopes_by_default(self, _init_messaging):
        # New default: only active scopes appear as send targets.
        recipients = messaging.list_recipients("*")
        assert "scope:proj-a/scope-2" not in recipients

    def test_recipients_include_completed_scopes_when_requested(self, _init_messaging):
        # Explicit opt-in restores the old behavior.
        recipients = messaging.list_recipients("*", include_inactive=True)
        assert "scope:proj-a/scope-2" in recipients

    def test_recipients_include_all_projects(self, _init_messaging):
        recipients = messaging.list_recipients("*")
        assert "project:proj-b" in recipients

    def test_recipients_deduplicate_scopes(self, _init_messaging):
        recipients = messaging.list_recipients("*")
        scope_entries = [r for r in recipients if r.startswith("scope:")]
        assert len(scope_entries) == len(set(scope_entries))

    def test_recipients_requires_query(self, _init_messaging):
        with pytest.raises(ValueError):
            messaging.list_recipients("")
        with pytest.raises(TypeError):
            messaging.list_recipients()  # type: ignore[call-arg]

    def test_recipients_regex_filter_scope_prefix(self, _init_messaging):
        recipients = messaging.list_recipients(r"^scope:proj-a/")
        assert recipients, "expected at least one match"
        assert all(r.startswith("scope:proj-a/") for r in recipients)

    def test_recipients_regex_filter_project_kind(self, _init_messaging):
        recipients = messaging.list_recipients(r"^project:")
        assert recipients
        assert all(r.startswith("project:") for r in recipients)

    def test_recipients_regex_filter_no_match(self, _init_messaging):
        assert messaging.list_recipients(r"^nonexistent-kind:") == []

    def test_recipients_invalid_regex_raises(self, _init_messaging):
        with pytest.raises(ValueError):
            messaging.list_recipients("[unterminated")

    def test_recipients_peer_arg_not_implemented(self, _init_messaging):
        with pytest.raises(NotImplementedError):
            messaging.list_recipients("*", peer="all")
        # local sentinels stay allowed
        messaging.list_recipients("*", peer=None)
        messaging.list_recipients("*", peer="local")


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_concurrent_writes(self, _init_messaging):
        errors = []

        def send(i):
            try:
                req = MessageSendRequest(
                    scope="workspace",
                    sender=f"agent-{i}",
                    msg_type="notification",
                    subject=f"Msg {i}",
                    body=f"Body {i}",
                )
                messaging.send_message(req)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=send, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        resp = messaging.search_messages(scope="workspace")
        assert resp.total >= 10
