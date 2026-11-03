"""Scope claims and the queue for a busy scope (scopes.md R3, R41, R42, R46).

A live session holds the claim on its scope, and the claim dies with the
session. awm keeps no claim on a card or anywhere else. A session counts as
live while its process runs, or while it waits on other sessions (R51), which
the harness's wait ledger reports. :func:`holder_live` is the one place that
rule lives.

reflection `cd` calls `scope_claim` before it moves a session and `scope_release`
for the scope it left. A claim found dead on read frees the scope at once and
hands it to the first live session in its queue.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from awm.scopes.dao import ScopesDAO
from awm.scopes.identity import now_ms

log = logging.getLogger("awm.scopes.claims")


PROC = Path("/proc")


def proc_start(pid: int) -> str | None:
    """The kernel start time of ``pid``, which tells a live process from a reused pid."""
    try:
        stat = (PROC / str(pid) / "stat").read_text()
    except OSError:
        return None
    # The command name sits in parentheses and may hold spaces; fields follow the last ')'.
    return stat.rsplit(")", 1)[1].split()[19]


def _waiting_sessions() -> set[str]:
    """Sessions the harness lists as waiting on other sessions (sessions.md R51)."""
    from awm import gatewayclient

    try:
        out = gatewayclient.call_sync("reflection", "waits", {}, timeout=10)
    except Exception:  # noqa: BLE001 — no wait ledger yet means nobody waits
        return set()
    rows = out.get("waits") if isinstance(out, dict) else None
    return {str(r.get("session_id")) for r in rows or [] if isinstance(r, dict) and r.get("session_id")}


#: Swapped in tests. Production asks the harness through the gateway.
waiting_sessions: Callable[[], set[str]] = _waiting_sessions


def _notify(job: str, text: str) -> bool:
    from awm import gatewayclient

    try:
        out = gatewayclient.call_sync("reflection", "message", {"job": job, "text": text}, timeout=60)
    except Exception as exc:  # noqa: BLE001 — a notice that fails leaves the waiter queued
        log.warning("scopes: could not tell %s its scope is free: %s", job, exc)
        return False
    return not (isinstance(out, dict) and out.get("ok") is False)


#: Swapped in tests. Production types the notice into the waiting session.
notify: Callable[[str, str], bool] = _notify


def holder_live(row: dict[str, Any], waiting: set[str] | None = None) -> bool:
    """Whether the session of a claim or queue row still counts as live (R3).

    Live means its process runs under the same start time, or the harness lists
    it as waiting on other sessions. Tony: "Waiting sessions hold scope".
    """
    pid = row.get("pid")
    if isinstance(pid, int) and pid > 0 and row.get("proc_start") and proc_start(pid) == row["proc_start"]:
        return True
    return row["session"] in (waiting if waiting is not None else waiting_sessions())


def _touch(dao: ScopesDAO, project: str, scope: str, conn: Any) -> None:
    dao.execute("INSERT INTO scope_use (project, scope, last_used) VALUES (?,?,?)"
                " ON CONFLICT (project, scope) DO UPDATE SET last_used=excluded.last_used",
                (project, scope, now_ms()), conn=conn)


def _holder(dao: ScopesDAO, project: str, scope: str, conn: Any, waiting: set[str]) -> dict | None:
    """The live claim on the scope. A dead one is dropped here."""
    row = dao.query_one("SELECT * FROM scope_claims WHERE project=? AND scope=?", (project, scope), conn=conn)
    if row is None:
        return None
    if holder_live(row, waiting):
        return row
    dao.execute("DELETE FROM scope_claims WHERE project=? AND scope=?", (project, scope), conn=conn)
    log.info("scopes: claim of %s/%s by %s ended with its session", project, scope, row["session"])
    return None


def _queue(dao: ScopesDAO, project: str, scope: str, conn: Any, waiting: set[str]) -> list[dict]:
    """The live waiters, first come first served (R46). Dead ones leave the queue."""
    rows = dao.query_all("SELECT * FROM scope_waiters WHERE project=? AND scope=? ORDER BY asked_at, rowid",
                         (project, scope), conn=conn)
    live = []
    for r in rows:
        if holder_live(r, waiting):
            live.append(r)
        else:
            dao.execute("DELETE FROM scope_waiters WHERE project=? AND scope=? AND session=?",
                        (project, scope, r["session"]), conn=conn)
    return live


def _public(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {"session_id": row["session"], "job": row.get("job"), "pid": row.get("pid"),
            "since": row.get("claimed_at") or row.get("asked_at")}


def _hand_over(project: str, scope: str, head: dict | None, conn: Any, dao: ScopesDAO) -> bool:
    """Tell the first waiter its scope is free, once."""
    if head is None or head.get("notified_at") or not head.get("job"):
        return False
    sent = notify(head["job"], f"[awm] scope {project}/{scope} is free and you are first in its queue. "
                               f"Move there with reflection cd now. Add to your question on the board "
                               f"that the conflict is clear (scopes.md R7).")
    if sent:
        dao.execute("UPDATE scope_waiters SET notified_at=? WHERE project=? AND scope=? AND session=?",
                    (now_ms(), project, scope, head["session"]), conn=conn)
    return sent


def claim(project: str, scope: str, session_id: str, *, pid: int | None = None, job: str | None = None,
          queue: bool = False, terminal: bool = False) -> dict:
    """Claim a scope for a session, or say who holds it.

    A terminal session (R41) is warned, never blocked or queued, and records no
    claim over the holder's. With ``queue`` a refused session joins the scope's
    queue and gets its place.
    """
    if not session_id:
        raise ValueError("session_id is required")
    start = proc_start(pid) if isinstance(pid, int) and pid > 0 else None
    waiting = waiting_sessions()
    dao = ScopesDAO()
    with dao.transaction() as conn:
        holder = _holder(dao, project, scope, conn, waiting)
        waiters = _queue(dao, project, scope, conn, waiting)
        ahead = [w for w in waiters if w["session"] != session_id]
        mine = next((w for w in waiters if w["session"] == session_id), None)
        if mine is not None:
            ahead = [w for w in waiters if (w["asked_at"], w["session"]) < (mine["asked_at"], mine["session"])]
        if holder is not None and holder["session"] != session_id:
            out = {"project": project, "scope": scope, "claimed": False, "holder": _public(holder)}
            if terminal:
                return {**out, "ok": True,
                        "warning": f"session {holder['session']} holds {project}/{scope}; you are not blocked"}
            if queue:
                if mine is None:
                    dao.execute("INSERT INTO scope_waiters (project, scope, session, pid, proc_start, job, asked_at)"
                                " VALUES (?,?,?,?,?,?,?)", (project, scope, session_id, pid, start, job, now_ms()),
                                conn=conn)
                return {**out, "ok": False, "queued_position": len(ahead) + 1}
            return {**out, "ok": False,
                    "reason": f"session {holder['session']} holds {project}/{scope}. "
                              f"Pass queue=true to wait for it, or ask Tony on the board (R7)."}
        if holder is None and ahead and not terminal:
            if queue and mine is None:
                dao.execute("INSERT INTO scope_waiters (project, scope, session, pid, proc_start, job, asked_at)"
                            " VALUES (?,?,?,?,?,?,?)", (project, scope, session_id, pid, start, job, now_ms()),
                            conn=conn)
            _hand_over(project, scope, ahead[0], conn, dao)
            return {"ok": False, "project": project, "scope": scope, "claimed": False, "holder": None,
                    "reason": f"{len(ahead)} session(s) asked for {project}/{scope} first",
                    "queued_position": len(ahead) + 1 if queue or mine else None}
        dao.execute("INSERT INTO scope_claims (project, scope, session, pid, proc_start, job, claimed_at)"
                    " VALUES (?,?,?,?,?,?,?) ON CONFLICT (project, scope) DO UPDATE SET"
                    " session=excluded.session, pid=excluded.pid, proc_start=excluded.proc_start,"
                    " job=excluded.job, claimed_at=excluded.claimed_at",
                    (project, scope, session_id, pid, start, job, now_ms()), conn=conn)
        dao.execute("DELETE FROM scope_waiters WHERE project=? AND scope=? AND session=?",
                    (project, scope, session_id), conn=conn)
        _touch(dao, project, scope, conn)
        return {"ok": True, "project": project, "scope": scope, "claimed": True,
                "holder": {"session_id": session_id, "job": job, "pid": pid}}


def release(project: str, scope: str, session_id: str) -> dict:
    """End a session's claim and hand the scope to the head of its queue."""
    waiting = waiting_sessions()
    dao = ScopesDAO()
    with dao.transaction() as conn:
        released = dao.execute("DELETE FROM scope_claims WHERE project=? AND scope=? AND session=?",
                               (project, scope, session_id), conn=conn) > 0
        if released:
            _touch(dao, project, scope, conn)
        holder = _holder(dao, project, scope, conn, waiting)
        waiters = _queue(dao, project, scope, conn, waiting)
        notified = holder is None and _hand_over(project, scope, waiters[0] if waiters else None, conn, dao)
    return {"ok": True, "project": project, "scope": scope, "released": released,
            "next": _public(waiters[0]) if waiters and holder is None else None, "notified": notified}


def withdraw(project: str, scope: str, session_id: str) -> dict:
    """Leave a scope's queue, as a session sent elsewhere or cancelled does (R46)."""
    dao = ScopesDAO()
    left = dao.execute("DELETE FROM scope_waiters WHERE project=? AND scope=? AND session=?",
                       (project, scope, session_id)) > 0
    return {"ok": True, "project": project, "scope": scope, "withdrawn": left}


def live_holder(project: str, scope: str) -> dict | None:
    """The live session that holds the scope, for the delete guard (R44 item 1)."""
    dao = ScopesDAO()
    with dao.transaction() as conn:
        return _public(_holder(dao, project, scope, conn, waiting_sessions()))


def listing(project: str | None = None, scope: str | None = None) -> dict:
    """Every live claim and queue, optionally for one project or scope."""
    waiting = waiting_sessions()
    dao = ScopesDAO()
    where, params = [], []
    for col, val in (("project", project), ("scope", scope)):
        if val:
            where.append(f"{col}=?")
            params.append(val)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    out = []
    with dao.transaction() as conn:
        keys = dao.query_all(f"SELECT project, scope FROM scope_claims{clause} UNION"
                             f" SELECT project, scope FROM scope_waiters{clause} ORDER BY project, scope",
                             params * 2, conn=conn)
        for k in keys:
            holder = _holder(dao, k["project"], k["scope"], conn, waiting)
            waiters = _queue(dao, k["project"], k["scope"], conn, waiting)
            if holder or waiters:
                out.append({"project": k["project"], "scope": k["scope"], "holder": _public(holder),
                            "queue": [_public(w) for w in waiters]})
    return {"claims": out, "count": len(out)}


def last_used(project: str, scope: str) -> int | None:
    row = ScopesDAO().query_one("SELECT last_used FROM scope_use WHERE project=? AND scope=?", (project, scope))
    return row["last_used"] if row else None


def bump(name: str) -> int:
    """Add one to a counter and return the new value."""
    dao = ScopesDAO()
    with dao.transaction() as conn:
        dao.execute("INSERT INTO scope_counters (name, value) VALUES (?, 1)"
                    " ON CONFLICT (name) DO UPDATE SET value=value+1", (name,), conn=conn)
        return dao.query_one("SELECT value FROM scope_counters WHERE name=?", (name,), conn=conn)["value"]
