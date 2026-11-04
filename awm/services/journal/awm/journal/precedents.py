"""The one-shot import of ``precedence.db`` into the journal (fleet.md R107 item 9).

A decision becomes a post tagged ``precedent`` on the workspace channel, its
body the context, the question and the decision. Its precedence tags join the
vocabulary. A note becomes a comment. A superseding decision replies to the one
it replaces, which gets ``superseded``, and a retired decision gets ``closed``.
Votes and the seen count are dropped. The source is opened read only, and each
post's id derives from the decision's, so a rerun adds nothing.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from awm.journal import builtin_tags, search_index, tags
from awm.journal.dao import JournalDAO
from awm.journal.refs import now_ms

CHANNEL = ("", "workspace")
AUTHOR = "system"
TAG_DESCRIPTION = "A topic tag on precedents, imported from precedence."
_NS = uuid.uuid5(uuid.NAMESPACE_URL, "awm:precedence-import")
_NOTE_LABEL = {"amendment": "Amendment", "context-change": "Context change",
               "supersede": "Supersede"}


def post_id(kind: str, source_id: str) -> str:
    return str(uuid.uuid5(_NS, f"{kind}:{source_id}"))


def body(context: str, question: str, decision: str) -> str:
    return f"Context: {context.strip()}\n\nQuestion: {question.strip()}\n\nDecision: {decision.strip()}"


def _ms(*stamps: str | None) -> int:
    for ts in stamps:
        if not ts:
            continue
        s = str(ts).strip()
        for parse in (datetime.fromisoformat, lambda v: datetime.strptime(v[:10], "%Y-%m-%d")):
            try:
                dt = parse(s.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp() * 1000)
    return now_ms()


def _tag_name(raw: str) -> str | None:
    try:
        return tags.normalize_name("-".join(str(raw).split()))
    except tags.TagError:
        return None


def import_db(source: Path, *, dry_run: bool = False) -> dict:
    """Import every decision, tag and note of ``source``; returns the counts."""
    if not source.exists():
        return {"status": "no-source", "source": str(source)}
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        decisions = src.execute("SELECT * FROM decisions ORDER BY created, added, id").fetchall()
        dtags = src.execute("SELECT decision_id, tag FROM tags").fetchall()
        notes = src.execute("SELECT * FROM notes ORDER BY created, id").fetchall()
    finally:
        src.close()

    names: dict[str, list[str]] = {}
    skipped_tags = set()
    for r in dtags:
        name = _tag_name(r["tag"])
        if name is None:
            skipped_tags.add(r["tag"])
        else:
            names.setdefault(r["decision_id"], []).append(name)
    new_tags = sorted({n for ns in names.values() for n in ns if tags.by_name(n) is None})

    by_id = {d["id"]: d for d in decisions}
    pid = {d["id"]: post_id("decision", d["id"]) for d in decisions}
    # A decision replaces the oldest one that names it; any other it replaces gets a comment.
    replaces: dict[str, list[str]] = {}
    for d in decisions:
        new = d["superseded_by"]
        if new and new in by_id and new != d["id"]:
            replaces.setdefault(new, []).append(d["id"])
    reply = {new: olds[0] for new, olds in replaces.items()}

    def root(did: str) -> str:
        seen = set()
        while did in reply and did not in seen:
            seen.add(did)
            did = reply[did]
        return did

    existing = {r["id"] for r in JournalDAO().query_all(
        "SELECT id FROM scope_posts WHERE id IN (%s)" % ",".join("?" * len(pid)),
        list(pid.values()))} if pid else set()
    out = {"status": "dry-run" if dry_run else "imported", "source": str(source),
           "decisions": len(decisions) - len(existing), "notes": len(notes),
           "tags_added": new_tags, "tags_skipped": sorted(skipped_tags),
           "already": len(existing)}
    if dry_run:
        return out

    for n in new_tags:
        tags.add(n, TAG_DESCRIPTION, by="precedence-import")
    rows, links, notes_out = [], [], []
    for d in decisions:
        if pid[d["id"]] in existing:
            continue
        top = root(d["id"])
        meta = {"title": (d["question"] or "")[:120], "precedence_id": d["id"],
                "created": d["created"], "source": d["source"], "source_ref": d["source_ref"]}
        rows.append((pid[d["id"]], *CHANNEL, AUTHOR,
                     body(d["context"] or "", d["question"] or "", d["decision"] or ""),
                     json.dumps({k: v for k, v in meta.items() if v}), _ms(d["created"], d["added"]),
                     pid[reply[d["id"]]] if d["id"] in reply else None,
                     pid[top] if top != d["id"] else None))
        ids = [builtin_tags.PRECEDENT, *(tags.by_name(n).id for n in names.get(d["id"], []))]
        if d["status"] == "superseded" or d["id"] in {o for olds in replaces.values() for o in olds}:
            ids.append(builtin_tags.SUPERSEDED)
        if d["status"] == "retired":
            ids.append(builtin_tags.CLOSED)
        links += [(pid[d["id"]], t) for t in dict.fromkeys(ids)]
        for extra in replaces.get(d["id"], [])[1:]:
            notes_out.append((post_id("supersede", f"{extra}>{d['id']}"), extra,
                              f"Superseded by {pid[d['id']]}.", AUTHOR, d["created"] or d["added"]))
    for n in notes:
        if n["decision_id"] in by_id:
            label = _NOTE_LABEL.get(n["kind"])
            notes_out.append((post_id("note", n["id"]), n["decision_id"],
                              f"{label}: {n['body']}" if label else n["body"],
                              n["author"] or AUTHOR, n["created"]))
    for nid, did, text, author, created in notes_out:
        rows.append((nid, *CHANNEL, author if ":" in author or author == "system" else f"user:{author}",
                     text, "{}", _ms(created), pid[did], pid[root(did)]))
        links.append((nid, builtin_tags.MESSAGE))

    dao = JournalDAO()
    with dao.transaction() as conn:
        d = JournalDAO(conn=conn)
        d.executemany("INSERT OR IGNORE INTO scope_posts (id, owner_project, owner_scope, author,"
                      " body, meta, ts, reply_to, thread_id) VALUES (?,?,?,?,?,?,?,?,?)", rows)
        d.executemany("INSERT OR IGNORE INTO post_tags (post_id, tag_id) VALUES (?,?)", links)
    for r in rows:
        if r[8] is None:
            search_index.index_post(r[0])
    return out
