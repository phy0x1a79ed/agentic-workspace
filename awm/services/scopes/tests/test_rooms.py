"""Tests for awm.scopes.rooms — create, archive, search, participants.

Ported from awm/tests/messaging/test_rooms_control.py (service-level tests only).
HTTP endpoint tests are omitted — awm.server is legacy monolith, not part of
this dist; those tests belong in gateway integration tests once the full
modular stack is up.

Key adaptations:
- ``from awm.db import get_connection`` → ``scopes_dao_conn`` fixture
- ``from awm.services import rooms as rooms_svc`` → ``from awm.scopes import rooms as rooms_svc``
- ``from awm.services import identity`` → ``from awm.scopes import identity``
- v1 schema: ``owner_project``/``owner_scope`` instead of ``owner_agent_id``;
  agent status updated via inline (project, scope) lookup through agents table.
"""

from __future__ import annotations

import pytest

from awm.scopes import rooms as rooms_svc
from awm.scopes.identity import ensure_project, ensure_agent, now_ms


pytestmark = [pytest.mark.messaging, pytest.mark.slow]


def _ensure_agent(scopes_workspace, scopes_dao_conn, project: str, scope: str,
                  *, status: str = "active") -> str:
    """Seed a project + agent row; return the agents.id uuid."""
    conn = scopes_dao_conn
    repo = str(scopes_workspace["projects_dir"] / project / ".bare")
    ensure_project(project, repo_path=repo, conn=conn)
    aid = ensure_agent(
        project, scope, branch=f"feat/{scope}",
        worktree=str(scopes_workspace["workspace"] / project / scope),
        status="allocated", conn=conn,
    )
    conn.execute(
        "UPDATE agents SET status=?, retired_at=? WHERE id=?",
        (status,
         None if status in ("allocated", "active") else now_ms(),
         aid),
    )
    conn.commit()
    return aid


# ---------------------------------------------------------------------------
# archive_room service tests
# ---------------------------------------------------------------------------

class TestArchiveRoomService:
    def test_archive_room_with_retired_owner(self, scopes_workspace, scopes_dao_conn):
        _ensure_agent(scopes_workspace, scopes_dao_conn, "awm", "dev", status="retired")
        room = rooms_svc.create_room(topic="empty", scopes=["awm/dev"])
        archived = rooms_svc.archive_room(room.id)
        assert archived.status == "archived"
        assert archived.closed_at is not None

    def test_archive_closed_room(self, scopes_workspace, scopes_dao_conn):
        _ensure_agent(scopes_workspace, scopes_dao_conn, "awm", "dev", status="retired")
        room = rooms_svc.create_room(topic="closed-first", scopes=["awm/dev"])
        rooms_svc.close_room(room.id)
        archived = rooms_svc.archive_room(room.id)
        assert archived.status == "archived"

    def test_archive_blocked_by_active_owner(self, scopes_workspace, scopes_dao_conn):
        _ensure_agent(scopes_workspace, scopes_dao_conn, "awm", "dev", status="active")
        room = rooms_svc.create_room(topic="busy", scopes=["awm/dev"])
        with pytest.raises(rooms_svc.RoomArchiveBlocked) as exc:
            rooms_svc.archive_room(room.id)
        assert "awm/dev" in exc.value.blocking_scopes

    def test_archive_allowed_after_owner_retires(self, scopes_workspace, scopes_dao_conn):
        aid = _ensure_agent(scopes_workspace, scopes_dao_conn, "awm", "dev", status="active")
        room = rooms_svc.create_room(topic="post-retire", scopes=["awm/dev"])
        # Retire the owner directly.
        scopes_dao_conn.execute(
            "UPDATE agents SET status='retired', retired_at=? WHERE id=?",
            (now_ms(), aid),
        )
        scopes_dao_conn.commit()
        archived = rooms_svc.archive_room(room.id)
        assert archived.status == "archived"

    def test_archive_missing_room_raises(self, scopes_workspace, scopes_dao_conn):
        with pytest.raises(rooms_svc.RoomNotFound):
            rooms_svc.archive_room("no-such-room")

    def test_archive_idempotent(self, scopes_workspace, scopes_dao_conn):
        _ensure_agent(scopes_workspace, scopes_dao_conn, "awm", "dev", status="retired")
        room = rooms_svc.create_room(topic="idem", scopes=["awm/dev"])
        rooms_svc.archive_room(room.id)
        again = rooms_svc.archive_room(room.id)
        assert again.status == "archived"

    def test_search_rooms_default_excludes_archived(self, scopes_workspace, scopes_dao_conn):
        _ensure_agent(scopes_workspace, scopes_dao_conn, "awm", "dev", status="retired")
        _ensure_agent(scopes_workspace, scopes_dao_conn, "awm", "api", status="retired")
        a = rooms_svc.create_room(topic="active-one", scopes=["awm/dev"])
        b = rooms_svc.create_room(topic="to-be-archived", scopes=["awm/api"])
        rooms_svc.archive_room(b.id)
        names = {r.id for r in rooms_svc.search_rooms()}
        assert a.id in names
        assert b.id not in names

    def test_search_rooms_status_archived(self, scopes_workspace, scopes_dao_conn):
        _ensure_agent(scopes_workspace, scopes_dao_conn, "awm", "dev", status="retired")
        room = rooms_svc.create_room(topic="arc", scopes=["awm/dev"])
        rooms_svc.archive_room(room.id)
        names = {r.id for r in rooms_svc.search_rooms(status="archived")}
        assert room.id in names


# ---------------------------------------------------------------------------
# Room CRUD basics
# ---------------------------------------------------------------------------

class TestRoomCreate:
    def test_create_basic(self, scopes_workspace, scopes_dao_conn):
        _ensure_agent(scopes_workspace, scopes_dao_conn, "proj-a", "scope-1")
        room = rooms_svc.create_room(topic="test-room", scopes=["proj-a/scope-1"])
        assert room.id
        assert room.status == "open"
        assert room.topic == "test-room"

    def test_get_room(self, scopes_workspace, scopes_dao_conn):
        _ensure_agent(scopes_workspace, scopes_dao_conn, "proj-a", "scope-1")
        room = rooms_svc.create_room(topic="get-test", scopes=["proj-a/scope-1"])
        fetched = rooms_svc.get_room(room.id)
        assert fetched is not None
        assert fetched.id == room.id

    def test_get_missing_room_returns_none(self, scopes_workspace):
        result = rooms_svc.get_room("no-such-room")
        assert result is None

    def test_post_message(self, scopes_workspace, scopes_dao_conn):
        _ensure_agent(scopes_workspace, scopes_dao_conn, "proj-a", "scope-1")
        room = rooms_svc.create_room(topic="chat", scopes=["proj-a/scope-1"])
        post = rooms_svc.post(room.id, author="user:op", body="hello")
        assert post.id
        assert post.body == "hello"

    def test_invite_participant(self, scopes_workspace, scopes_dao_conn):
        _ensure_agent(scopes_workspace, scopes_dao_conn, "proj-a", "scope-1")
        _ensure_agent(scopes_workspace, scopes_dao_conn, "proj-a", "scope-2")
        room = rooms_svc.create_room(topic="invite-test", scopes=["proj-a/scope-1"])
        p = rooms_svc.add_participant(room.id, "scope", "proj-a/scope-2")
        assert p is not None

    def test_close_room(self, scopes_workspace, scopes_dao_conn):
        _ensure_agent(scopes_workspace, scopes_dao_conn, "proj-a", "scope-1")
        room = rooms_svc.create_room(topic="close-me", scopes=["proj-a/scope-1"])
        closed = rooms_svc.close_room(room.id)
        assert closed.status == "closed"


# ---------------------------------------------------------------------------
# Cross-service RPCs (unit tests for the new room helpers)
# ---------------------------------------------------------------------------

class TestCrossServiceRPCs:
    def test_ensure_agent_room_creates(self, scopes_workspace, scopes_dao_conn):
        _ensure_agent(scopes_workspace, scopes_dao_conn, "proj-a", "scope-1")
        room_id = rooms_svc.ensure_agent_room("proj-a", "scope-1")
        assert room_id
        room = rooms_svc.get_room(room_id)
        assert room is not None
        assert room.status == "open"

    def test_ensure_agent_room_idempotent(self, scopes_workspace, scopes_dao_conn):
        _ensure_agent(scopes_workspace, scopes_dao_conn, "proj-a", "scope-1")
        id1 = rooms_svc.ensure_agent_room("proj-a", "scope-1")
        id2 = rooms_svc.ensure_agent_room("proj-a", "scope-1")
        assert id1 == id2

    def test_rooms_for_scope_as_owner(self, scopes_workspace, scopes_dao_conn):
        _ensure_agent(scopes_workspace, scopes_dao_conn, "proj-a", "scope-1")
        rooms_svc.create_room(topic="room-a", scopes=["proj-a/scope-1"])
        room_ids = rooms_svc.rooms_for_scope("proj-a/scope-1")
        assert len(room_ids) >= 1

    def test_rooms_for_scope_as_guest(self, scopes_workspace, scopes_dao_conn):
        _ensure_agent(scopes_workspace, scopes_dao_conn, "proj-a", "scope-1")
        _ensure_agent(scopes_workspace, scopes_dao_conn, "proj-a", "scope-2")
        room = rooms_svc.create_room(topic="guest-room", scopes=["proj-a/scope-1"])
        rooms_svc.add_participant(room.id, "scope", "proj-a/scope-2")
        room_ids = rooms_svc.rooms_for_scope("proj-a/scope-2")
        assert room.id in room_ids

    def test_auto_close_for_scope(self, scopes_workspace, scopes_dao_conn):
        _ensure_agent(scopes_workspace, scopes_dao_conn, "proj-a", "scope-1")
        room = rooms_svc.create_room(topic="auto-close", scopes=["proj-a/scope-1"])
        closed = rooms_svc.auto_close_for_scope("proj-a/scope-1")
        assert room.id in closed
        # Closed room should no longer appear in active list.
        active = rooms_svc.search_rooms(status="active")
        assert room.id not in {r.id for r in active}

    def test_room_agents_kill_on_close(self, scopes_workspace, scopes_dao_conn):
        _ensure_agent(scopes_workspace, scopes_dao_conn, "proj-a", "scope-1")
        _ensure_agent(scopes_workspace, scopes_dao_conn, "proj-a", "scope-2")
        room = rooms_svc.create_room(topic="kill-on-close", scopes=["proj-a/scope-1"])
        rooms_svc.add_participant(room.id, "scope", "proj-a/scope-2")
        pairs = rooms_svc.room_agents_kill_on_close(room.id)
        scopes_in_pairs = [f"{p}/{s}" for p, s in pairs]
        assert "proj-a/scope-1" in scopes_in_pairs
        assert "proj-a/scope-2" in scopes_in_pairs
