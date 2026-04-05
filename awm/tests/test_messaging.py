"""Tests for the messaging service."""

from __future__ import annotations

import threading

import pytest

from awm.models import MessageSendRequest
from awm.services import messaging


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def _init_messaging(awm_workspace, seeded_tasks):
    """Ensure DB and tasks are ready for messaging tests."""
    return awm_workspace


@pytest.fixture()
def seeded_messages(_init_messaging, db_conn):
    """Insert sample messages for search/filter tests."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    rows = [
        ("workspace", "agent-1", "status_update", "Build complete", "All tests passed", None, "unread", now, None),
        ("project:proj-a", "agent-2", "notification", "Review needed", "PR #42 ready", None, "unread", now, None),
        ("task:proj-a/task-1", "workspace", "task_assignment", "Implement feature X", "See the spec below", '{"priority": "high"}', "unread", now, None),
        ("task:proj-a/task-1", "workspace", "plan", "Execution plan", "Step 1: do the thing", None, "read", now, now),
    ]
    for r in rows:
        db_conn.execute(
            "INSERT INTO messages (scope, sender, msg_type, subject, body, metadata, status, created_at, read_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            r,
        )
    db_conn.commit()
    return rows


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

    def test_send_to_task_scope(self, _init_messaging):
        req = MessageSendRequest(
            scope="task:proj-a/task-1",
            sender="workspace",
            msg_type="plan",
            subject="Plan",
            body="Do this",
        )
        resp = messaging.send_message(req)
        assert resp.msg.scope == "task:proj-a/task-1"

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
        for msg_type in ["task_assignment", "reflection", "status_update", "notification", "plan"]:
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
        resp = messaging.search_messages(msg_type="task_assignment")
        assert resp.total == 1

    def test_search_by_query(self, seeded_messages):
        resp = messaging.search_messages(query="tests passed")
        assert resp.total == 1
        assert "tests passed" in resp.messages[0].body.lower()

    def test_search_query_in_subject(self, seeded_messages):
        resp = messaging.search_messages(query="Review needed")
        assert resp.total == 1

    def test_search_with_limit(self, seeded_messages):
        resp = messaging.search_messages(limit=2)
        assert resp.total == 2

    def test_search_combined_filters(self, seeded_messages):
        resp = messaging.search_messages(scope="task:proj-a/task-1", status="unread")
        assert resp.total == 1
        assert resp.messages[0].msg_type == "task_assignment"

    def test_search_invalid_scope(self, seeded_messages):
        with pytest.raises(ValueError, match="Invalid scope"):
            messaging.search_messages(scope="bad scope")


# ---------------------------------------------------------------------------
# read_inbox
# ---------------------------------------------------------------------------

class TestReadInbox:
    def test_read_inbox_returns_unread(self, seeded_messages):
        resp = messaging.read_inbox(scope="task:proj-a/task-1")
        # Only the unread task_assignment (not the already-read plan)
        assert resp.total == 1
        assert resp.messages[0].msg_type == "task_assignment"
        assert resp.messages[0].status == "read"  # marked read by the call

    def test_read_inbox_marks_as_read(self, seeded_messages, db_conn):
        messaging.read_inbox(scope="workspace")
        row = db_conn.execute(
            "SELECT status FROM messages WHERE scope = 'workspace'"
        ).fetchone()
        assert row["status"] == "read"

    def test_read_inbox_scope_required(self, seeded_messages):
        with pytest.raises(TypeError):
            messaging.read_inbox()  # type: ignore[call-arg]

    def test_read_inbox_invalid_scope(self, seeded_messages):
        with pytest.raises(ValueError, match="Invalid scope"):
            messaging.read_inbox(scope="bad")

    def test_read_inbox_with_status_filter(self, seeded_messages):
        resp = messaging.read_inbox(scope="task:proj-a/task-1", status="read")
        assert resp.total == 1
        assert resp.messages[0].msg_type == "plan"

    def test_read_inbox_empty(self, seeded_messages):
        resp = messaging.read_inbox(scope="project:proj-b")
        assert resp.total == 0


# ---------------------------------------------------------------------------
# list_recipients
# ---------------------------------------------------------------------------

class TestListRecipients:
    def test_recipients_include_workspace(self, _init_messaging):
        recipients = messaging.list_recipients()
        assert "workspace" in recipients

    def test_recipients_include_active_projects(self, _init_messaging):
        recipients = messaging.list_recipients()
        assert "project:proj-a" in recipients

    def test_recipients_include_active_tasks(self, _init_messaging):
        recipients = messaging.list_recipients()
        assert "task:proj-a/task-1" in recipients

    def test_recipients_include_completed_tasks(self, _init_messaging):
        recipients = messaging.list_recipients()
        # task-2 is completed in seeded_tasks — should still be a recipient
        assert "task:proj-a/task-2" in recipients

    def test_recipients_include_all_projects(self, _init_messaging):
        recipients = messaging.list_recipients()
        # proj-b only has completed tasks but should still appear
        assert "project:proj-b" in recipients

    def test_recipients_deduplicate_tasks(self, _init_messaging):
        recipients = messaging.list_recipients()
        # Each task should appear exactly once
        task_entries = [r for r in recipients if r.startswith("task:")]
        assert len(task_entries) == len(set(task_entries))


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
