"""Tests for awm.services.locks — acquire, release, heartbeat, reap, conflicts."""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.misc, pytest.mark.smoke]

import os
from datetime import datetime, timedelta, timezone

import pytest

from awm.models import LockAcquireRequest
from awm.services import locks


class TestAcquire:
    def test_acquire_exclusive(self, awm_workspace):
        req = LockAcquireRequest(
            resource_path="projects/proj/scope-1", holder_id="agent-1",
            holder_pid=os.getpid(),
        )
        result = locks.acquire(req)
        assert result.lock is not None
        assert result.lock.lock_type == "exclusive"
        assert result.lock.holder_id == "agent-1"

    def test_acquire_shared(self, awm_workspace):
        req = LockAcquireRequest(
            resource_path="data/shared.csv", holder_id="agent-1",
            lock_type="shared",
        )
        result = locks.acquire(req)
        assert result.lock is not None
        assert result.lock.lock_type == "shared"

    def test_idempotent_reacquire(self, awm_workspace):
        req = LockAcquireRequest(
            resource_path="projects/proj/scope-1", holder_id="agent-1",
        )
        r1 = locks.acquire(req)
        r2 = locks.acquire(req)
        assert r1.lock is not None
        assert r2.lock is not None
        assert r1.lock.resource_path == r2.lock.resource_path

    def test_two_shared_on_same_path(self, awm_workspace):
        req1 = LockAcquireRequest(
            resource_path="data/shared.csv", holder_id="agent-1",
            lock_type="shared",
        )
        req2 = LockAcquireRequest(
            resource_path="data/shared.csv", holder_id="agent-2",
            lock_type="shared",
        )
        r1 = locks.acquire(req1)
        r2 = locks.acquire(req2)
        assert r1.lock is not None
        assert r2.lock is not None


class TestConflicts:
    def test_exclusive_vs_exclusive_same_path(self, awm_workspace):
        req1 = LockAcquireRequest(resource_path="file.txt", holder_id="a1")
        req2 = LockAcquireRequest(resource_path="file.txt", holder_id="a2")
        locks.acquire(req1)
        r2 = locks.acquire(req2)
        assert r2.lock is None
        assert "Conflict" in r2.message

    def test_exclusive_vs_shared_same_path(self, awm_workspace):
        req1 = LockAcquireRequest(resource_path="file.txt", holder_id="a1")
        req2 = LockAcquireRequest(resource_path="file.txt", holder_id="a2", lock_type="shared")
        locks.acquire(req1)
        r2 = locks.acquire(req2)
        assert r2.lock is None

    def test_parent_child_conflict(self, awm_workspace):
        """Locking a parent should conflict with locking a child."""
        req1 = LockAcquireRequest(resource_path="projects/proj", holder_id="a1")
        req2 = LockAcquireRequest(resource_path="projects/proj/scope-1", holder_id="a2")
        locks.acquire(req1)
        r2 = locks.acquire(req2)
        assert r2.lock is None

    def test_child_parent_conflict(self, awm_workspace):
        """Locking a child should conflict with locking the parent."""
        req1 = LockAcquireRequest(resource_path="projects/proj/scope-1", holder_id="a1")
        req2 = LockAcquireRequest(resource_path="projects/proj", holder_id="a2")
        locks.acquire(req1)
        r2 = locks.acquire(req2)
        assert r2.lock is None


class TestRelease:
    def test_release_existing(self, awm_workspace):
        req = LockAcquireRequest(resource_path="file.txt", holder_id="a1")
        locks.acquire(req)
        result = locks.release("file.txt", "a1")
        assert result.lock is not None
        assert "released" in result.message.lower()

    def test_release_nonexistent(self, awm_workspace):
        result = locks.release("no-such-file", "no-agent")
        assert result.lock is None
        assert "No matching" in result.message


class TestHeartbeat:
    def test_heartbeat_updates(self, awm_workspace):
        req = LockAcquireRequest(resource_path="file.txt", holder_id="a1")
        locks.acquire(req)
        result = locks.heartbeat("a1")
        assert "1 lock(s)" in result.message

    def test_heartbeat_no_locks(self, awm_workspace):
        result = locks.heartbeat("nobody")
        assert "0 lock(s)" in result.message


class TestListLocks:
    def test_list_all(self, awm_workspace, seeded_locks):
        result = locks.list_locks()
        assert result.total == 3

    def test_list_by_holder(self, awm_workspace, seeded_locks):
        result = locks.list_locks(holder_id="agent-1")
        assert result.total == 2

    def test_list_by_path(self, awm_workspace, seeded_locks):
        result = locks.list_locks(path="data/shared.csv")
        assert result.total == 1


class TestReap:
    def test_reap_dead_pid(self, awm_workspace, db_conn, monkeypatch):
        """Lock with dead PID should be reaped."""
        now = datetime.now(timezone.utc).isoformat()
        db_conn.execute(
            "INSERT INTO locks (resource_path, holder_id, holder_pid, lock_type, acquired_at, heartbeat_at) VALUES (?,?,?,?,?,?)",
            ("file.txt", "dead-agent", 999999, "exclusive", now, now),
        )
        db_conn.commit()
        monkeypatch.setattr("awm.services.locks._pid_alive", lambda pid: False)
        reaped = locks.reap_stale()
        assert reaped == 1

    def test_reap_old_heartbeat(self, awm_workspace, db_conn, monkeypatch):
        """Lock with stale heartbeat and no PID should be reaped."""
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
        db_conn.execute(
            "INSERT INTO locks (resource_path, holder_id, holder_pid, lock_type, acquired_at, heartbeat_at) VALUES (?,?,?,?,?,?)",
            ("file.txt", "stale-agent", None, "exclusive", old_time, old_time),
        )
        db_conn.commit()
        reaped = locks.reap_stale()
        assert reaped == 1

    def test_reap_fresh_lock_untouched(self, awm_workspace, db_conn, monkeypatch):
        """Fresh lock with living PID should NOT be reaped."""
        now = datetime.now(timezone.utc).isoformat()
        db_conn.execute(
            "INSERT INTO locks (resource_path, holder_id, holder_pid, lock_type, acquired_at, heartbeat_at) VALUES (?,?,?,?,?,?)",
            ("file.txt", "alive-agent", os.getpid(), "exclusive", now, now),
        )
        db_conn.commit()
        reaped = locks.reap_stale()
        assert reaped == 0

    def test_reap_returns_count(self, awm_workspace, db_conn, monkeypatch):
        """Reap should return the count of reaped locks."""
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
        for i in range(3):
            db_conn.execute(
                "INSERT INTO locks (resource_path, holder_id, holder_pid, lock_type, acquired_at, heartbeat_at) VALUES (?,?,?,?,?,?)",
                (f"file-{i}.txt", f"stale-{i}", None, "exclusive", old_time, old_time),
            )
        db_conn.commit()
        reaped = locks.reap_stale()
        assert reaped == 3
