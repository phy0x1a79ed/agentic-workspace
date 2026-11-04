"""The fleet's one tag vocabulary: each node's copy, kept in step (fleet.md R107 item 6).

An add or an edit on one node goes to every peer by itself, and a pull at start
and every few hours heals a push that failed, so the copies are eventually
consistent. A tag keeps its id everywhere; the higher version of an id wins.

Two nodes that add the same name before they sync hold two concepts. On every
node the newer tag, by its add time, takes a provisional name, and the node that
added it dispatches an agent to choose the real one. While the clash stands, the
two nodes refuse each other's journal reads, which blocks search between them.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from typing import Any, Callable

from awm.journal.dao import JournalDAO
from awm.journal.refs import now_ms

log = logging.getLogger("awm.journal.tagsync")

PULL_EVERY_S = 6 * 3600
_PROVISIONAL_RE = re.compile(r"\.clash-[0-9a-f]{6}$")


def provisional(name: str) -> bool:
    return bool(_PROVISIONAL_RE.search(name or ""))


def provisional_name(name: str, tag_id: str) -> str:
    """The name the newer tag of a clash holds until an agent renames it. Every node derives the same one."""
    suffix = ".clash-" + re.sub(r"[^0-9a-f]", "", tag_id.lower()).ljust(6, "0")[:6]
    return name[:64 - len(suffix)] + suffix


# ---------------------------------------------------------------------------
# Out: push to every peer
# ---------------------------------------------------------------------------

def peers() -> list[str]:
    """The nodes this node keeps the vocabulary in step with: those it holds an edge to."""
    try:
        from awm.config import edges
        return edges.reachable_from(edges.here())
    except Exception as exc:  # noqa: BLE001 — no edge list means a node alone
        log.info("no peers to sync tags with: %s", exc)
        return []


def _send(peer: str, tags: list[dict]) -> None:
    try:
        from awm.gatewayclient import call_sync
        call_sync("journal", "tag_receive", {"tags": tags}, peer=peer, timeout=15.0)
    except Exception as exc:  # noqa: BLE001 — the next pull heals it
        log.info("tag sync to %s failed, the next pull heals it: %s", peer, exc)


#: Replaced in tests. Runs each send off the caller's thread.
sender: Callable[[str, list[dict]], None] = lambda peer, tags: threading.Thread(
    target=_send, args=(peer, tags), daemon=True, name=f"tagsync-{peer}").start()


def push(change: dict) -> None:
    tag = change.get("tag")
    if not tag:
        return
    for peer in peers():
        sender(peer, [tag])


# ---------------------------------------------------------------------------
# In: merge a peer's tags
# ---------------------------------------------------------------------------

def _local_node() -> str:
    from awm.journal import tags
    return tags._node()


def _newer(a: dict, b: dict) -> bool:
    """Whether tag ``a`` was added after tag ``b``; the id breaks a tie."""
    return (int(a.get("created_ms") or 0), a["id"]) > (int(b.get("created_ms") or 0), b["id"])


def merge(remote: list[dict], *, from_node: str = "") -> dict:
    """Fold a peer's tags into this node's vocabulary; returns what changed."""
    from awm.journal import tags
    out = {"added": 0, "updated": 0, "clashes": 0}
    for raw in remote or []:
        if not isinstance(raw, dict) or not raw.get("id") or not raw.get("name"):
            continue
        try:
            name = tags.normalize_name(raw["name"])
        except tags.TagError:
            log.warning("tag %s from %s has a name this node refuses: %r",
                        raw["id"], from_node, raw["name"])
            continue
        incoming = {**raw, "name": name, "description": str(raw.get("description") or name),
                    "origin": str(raw.get("origin") or from_node),
                    "version": int(raw.get("version") or 1)}
        local = tags.get(incoming["id"])
        if local is not None and incoming["version"] <= local.version:
            continue
        holder = tags.by_name(_PROVISIONAL_RE.sub("", name))
        if holder is not None and holder.id != incoming["id"] and not provisional(name):
            out["clashes"] += 1
            name = _clash(holder.to_dict(), incoming)
            if name is None:
                continue
        elif (other := tags.by_name(name)) is not None and other.id != incoming["id"]:
            log.warning("tag %s from %s names %r, which %s holds here", incoming["id"],
                        from_node, name, other.id)
            continue
        _store(incoming, name, existed=local is not None)
        out["updated" if local is not None else "added"] += 1
        if local is not None and local.name != name:
            resolved_by_edit(incoming["id"])
    return out


def _store(tag: dict, name: str, *, existed: bool) -> None:
    now = now_ms()
    dao = JournalDAO()
    with dao.transaction() as conn:
        d = JournalDAO(conn=conn)
        if existed:
            d.execute("UPDATE tags SET name=?, description=?, version=?, updated_at=? WHERE id=?",
                      (name, tag["description"], tag["version"], now, tag["id"]))
        else:
            d.execute("INSERT INTO tags (id, name, description, origin, version, created_at,"
                      " updated_at) VALUES (?,?,?,?,?,?,?)",
                      (tag["id"], name, tag["description"], tag["origin"], tag["version"],
                       int(tag.get("created_ms") or now), now))
        d.execute("INSERT OR IGNORE INTO tag_versions (tag_id, version, name, description,"
                  " edited_at, edited_by) VALUES (?,?,?,?,?,?)",
                  (tag["id"], tag["version"], tag["name"], tag["description"], now,
                   f"sync:{tag['origin']}"))


def _clash(holder: dict, incoming: dict) -> str | None:
    """Record a clash between the tag here and one from a peer under the same name.

    Returns the name to store ``incoming`` under, or None when it should wait.
    """
    from awm.journal import builtin_tags
    if holder["id"] in builtin_tags.BUILTINS and incoming["id"] in builtin_tags.BUILTINS:
        return None
    newer, older = (incoming, holder) if _newer(incoming, holder) else (holder, incoming)
    base = holder["name"]
    now = now_ms()
    dao = JournalDAO()
    with dao.transaction() as conn:
        d = JournalDAO(conn=conn)
        d.execute("INSERT OR IGNORE INTO tag_clashes (name, older_id, newer_id, older_node,"
                  " newer_node, detected_at) VALUES (?,?,?,?,?,?)",
                  (base, older["id"], newer["id"], older.get("origin") or "",
                   newer.get("origin") or "", now))
        if newer is holder:
            d.execute("UPDATE tags SET name=?, updated_at=? WHERE id=?",
                      (provisional_name(base, holder["id"]), now, holder["id"]))
    log.warning("tag clash on %r: %s (%s) is newer than %s (%s)", base, newer["id"],
                newer.get("origin"), older["id"], older.get("origin"))
    if (newer.get("origin") or "") == _local_node():
        dispatch_open()
    return base if newer is holder else provisional_name(base, incoming["id"])


def resolved_by_edit(tag_id: str) -> None:
    """A rename of the newer tag of a clash settles it."""
    JournalDAO().execute(
        "UPDATE tag_clashes SET resolved_at=? WHERE newer_id=? AND resolved_at IS NULL",
        (now_ms(), tag_id))


def clashes(*, open_only: bool = True) -> list[dict]:
    where = " WHERE resolved_at IS NULL" if open_only else ""
    return JournalDAO().query_all(f"SELECT * FROM tag_clashes{where} ORDER BY detected_at")


def blocked(peer: str | None) -> bool:
    """Whether an open clash stands between this node and ``peer``, so their reads are refused."""
    if not peer:
        return False
    here = _local_node()
    for c in clashes():
        if {c["older_node"], c["newer_node"]} == {here, peer}:
            return True
    return False


# ---------------------------------------------------------------------------
# The agent that chooses the name
# ---------------------------------------------------------------------------

def _prompt(c: dict) -> str:
    from awm.journal import tags
    newer, older = tags.get(c["newer_id"]), tags.get(c["older_id"])
    return (
        f"Two nodes added the journal tag name {c['name']!r} before they synced, so it "
        f"names two concepts (fleet.md R107 item 6). The older tag {c['older_id']} from "
        f"{c['older_node'] or 'a peer'} keeps the name: "
        f"{older.description if older else '(not here)'!r}. The newer tag {c['newer_id']} "
        f"from this node is {newer.name if newer else '?'!r} for now: "
        f"{newer.description if newer else ''!r}. Read posts that carry it with "
        f"journal(verb=\"fetch\", args={{tags:{(newer.name if newer else '')!r}}}). "
        "Choose a name that says what the newer tag means and is not in tag_list, then "
        f"run journal(verb=\"tag_edit\", args={{tag:\"{c['newer_id']}\", name:<the name>}}). "
        "That rename reaches every node and ends the clash. Change nothing else, then stop."
    )


def _start_agent(c: dict) -> str:
    from awm.gatewayclient import call_sync
    res = call_sync("reflection", "start_other",
                    {"name": f"tag-clash-{c['newer_id'][:8]}", "prompt": _prompt(c),
                     "effort": "low"}, timeout=60.0)
    return str((res or {}).get("job") or (res or {}).get("job_id") or res)


#: Replaced in tests. Starts the agent for one clash and returns a handle to record.
dispatcher: Callable[[dict], str] = _start_agent


def dispatch_open() -> list[dict]:
    """Dispatch an agent for each open clash whose newer tag this node added, once each."""
    here, out = _local_node(), []
    for c in clashes():
        if c["dispatched_at"] or c["newer_node"] != here:
            continue
        try:
            handle = dispatcher(c)
        except Exception as exc:  # noqa: BLE001 — retried on the next pull
            log.warning("cannot dispatch the agent for the %r clash: %s", c["name"], exc)
            continue
        JournalDAO().execute(
            "UPDATE tag_clashes SET dispatched_at=?, dispatch=? WHERE older_id=? AND newer_id=?",
            (now_ms(), handle, c["older_id"], c["newer_id"]))
        out.append({**c, "dispatch": handle})
    return out


# ---------------------------------------------------------------------------
# Pull: heal what a push missed
# ---------------------------------------------------------------------------

def pull(fetch: Callable[[str], list[dict]] | None = None) -> dict:
    """Merge every peer's vocabulary into this node's; returns the counts per peer."""
    def _fetch(peer: str) -> list[dict]:
        from awm.gatewayclient import call_sync
        return (call_sync("journal", "tag_list", {}, peer=peer, timeout=15.0) or {}).get("tags", [])
    fetch = fetch or _fetch
    out = {}
    for peer in peers():
        try:
            out[peer] = merge(fetch(peer), from_node=peer)
        except Exception as exc:  # noqa: BLE001
            out[peer] = {"error": repr(exc)[:200]}
    dispatch_open()
    return out


async def pull_loop() -> None:
    """Pull now and every :data:`PULL_EVERY_S`; runs under ``spawn_supervised``."""
    while True:
        counts = await asyncio.to_thread(pull)
        log.info("tag pull: %s", counts)
        await asyncio.sleep(PULL_EVERY_S)


def receive(tags: Any, as_: str | None) -> dict:
    """A peer's pushed tags. Only an edge-stamped peer may send them."""
    from awm import config
    node = config.caller_peer(as_) if isinstance(as_, str) else None
    if not node:
        raise PermissionError("tag_receive takes tags from a peer node only")
    return merge(tags if isinstance(tags, list) else [], from_node=node)
