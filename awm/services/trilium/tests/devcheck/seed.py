"""Seed the dev vault: pages, layout migration, synthetic cards, a roster. Env awm, AWM_VAULT_PORT=37750."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.board_stubs import card  # noqa: E402

from awm.trilium.board import ids, service  # noqa: E402
from awm.trilium.board.fence import Fence  # noqa: E402

now = time.time()
fence = Fence()
nodes = [
    {"node": "mizar", "role": "compute", "last_seen": now - 30, "sessions_7d": 41,
     "sessions": [{"session_id": "ccdaf676e8000040008000000000000000", "name": "plugin-redux/trilium-vault", "link": "https://claude.ai/code/session_aaa",
                   "scope": "awm/trilium-vault", "remote_control": True}],
     "uses": [{"tool": "journal", "host": "alcor", "listed": True, "last_checked": now - 5, "last_used": now - 100}]},
    {"node": "lyra", "role": "compute", "last_seen": now - 4000, "sessions_7d": 3,
     "sessions": [{"session_id": "cc9f000000000040008000000000000000", "name": "lab notes", "scope": "lab/x"}], "uses": []},
    {"node": "altair", "role": "compute", "last_seen": now - 90, "sessions_7d": 9, "sessions": []},
    {"node": "capella", "role": "station", "last_seen": now - 9000, "sessions_7d": 0, "sessions": []},
    {"node": "deneb", "role": "front", "last_seen": now - 2, "sessions_7d": 0, "sessions": []},
]
services = [{"tool": "journal", "service": "journal", "host": "alcor", "health": "up", "last_response": now - 3, "last_used": now - 60},
                  {"tool": "trilium", "service": "trilium", "host": "deneb", "health": "down", "last_response": now - 7200, "last_used": now - 7000}]
seats = [{"seat": "acct/111", "name": "seat:discord/lab/steward", "state": "occupied", "remote_control": True,
                "model": "opus", "effort": "high", "refresh": False, "rc_url": "https://claude.ai/code/session_seat", "node": "mizar"},
               {"seat": "acct/222", "name": "seat:slack/lab/triage", "state": "empty", "remote_control": False,
                "model": "sonnet", "effort": "medium", "refresh": False}]
notices = [{"kind": "scope-hygiene", "node": "mizar", "title": "Scope hygiene", "body": "2 removed", "ts": now}]

cards = []

# One ordinary note to start a task from: the first child of Tony's book that is not ours.
c = fence.c
source = c.note(c.note("qUFGgLTobIn8")["childNoteIds"][0])
print("source note:", source["noteId"], "|", source["title"])

cards.append(card("stub-needs", "needs_you", status="blocked", meta={"reason": "needs a decision"},
                sessions=[{"session_id": "s1", "name": "worker a", "role": "main", "active": True, "node": "mizar",
                           "rc_url": "https://claude.ai/code/session_aaa", "started": now - 600}],
                approvals=[{"request_id": "req-1", "state": "pending", "summary": "ssh to fir\nfor 10 min", "requested_at": now - 60},
                           {"request_id": "req-0", "state": "approved", "summary": "earlier thing", "requested_at": now - 900, "decided_at": now - 800}],
                source_note=source["noteId"], updated=now))
cards.append(card("stub-work", "working", peer="lyra", meta={"todos": [{"subject": "do it", "status": "completed"}, {"subject": "check", "status": "pending"}]},
                sessions=[{"session_id": "s2", "name": "worker b", "role": "main", "active": True, "node": "lyra", "rc_url": "", "started": now - 90}], updated=now - 5))
cards.append(card("stub-stale", "stale", status="planned", peer="altair", sessions=[
    {"session_id": "s3", "name": "dead", "role": "main", "active": False, "node": "altair", "rc_url": "https://claude.ai/code/session_dead", "started": now - 99999}], updated=now - 9999))
for i in range(70):
    cards.append(card(f"stub-hist-{i:02d}", "history", peer="mizar", done_at=int(now) - 10000 - i * 60, updated=now - 20000 - i, meta={}))

board = service.Board(fence)
base = board.apply({})["mark"]
print("applied:", board.apply({"base": base, "cards": cards, "mark": base + len(cards)}))
board.render({"nodes": nodes, "services": services, "seats": seats, "notices": notices})
roster = board.ops._stored()
print("agent_nodes:", roster["agent_nodes"])
print("origin:", [a["value"] for a in fence.note(ids.ROOT_ID)["attributes"] if a["name"] == "alcorOrigin"])
