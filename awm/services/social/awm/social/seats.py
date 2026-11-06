"""Seats: a chat a bot account is in, held by one Claude Code background session.

A mount is a ``social_mounts`` row on the node that owns the account. Each new
message in a mounted seat is typed into the holder's prompt through
``reflection message`` on the holder's node, one line per message, in order per
holder (``awm.gatewayclient.push``, channels.md R69). A seat is released by its
holder, taken by a forced mount, or freed by the prune once the holder's job is gone.

The holder is never named by the model: ``awm.gatewayclient.holders`` reads it
from what the gateway stamps, as it does for a channel.
"""

from __future__ import annotations

import asyncio
import logging

from awm.gatewayclient.holders import HolderError as SeatError
from awm.gatewayclient.push import live_jobs
from awm.social.connectors.base import InboundMessage

log = logging.getLogger("awm.social.seats")

PRUNE_S = 60.0
TEXT_MAX = 1500


def session_name(platform: str, chat) -> str:
    """The name a seat's holder goes by: ``seat:<app>/<server>/<channel>``, or ``seat:<app>/DM/<person>``."""
    where = f"DM/{chat.name}" if chat.kind == "dm" else (chat.name or chat.id).replace("#", "/", 1)
    return f"seat:{platform}/{where}"


def holder_label(row: dict) -> str:
    name = f" ({row['session_name']})" if row.get("session_name") else ""
    return f"{row['job']}@{row['node']}{name}"


# -- trust -------------------------------------------------------------------

def seat_trust(account_cfg, channel_id: str) -> str:  # noqa: ANN001
    """The trust level of the seat on ``channel_id`` (``channels.md`` R70)."""
    if account_cfg is None:
        return "untrusted"
    if channel_id in account_cfg.trusted_chats:
        return "trusted"
    return account_cfg.trust


def held_chats(dao, account: str, holder: dict | None) -> set[str]:  # noqa: ANN001
    """The chats of ``account`` that ``holder`` holds."""
    if holder is None:
        return set()
    return {r["channel_id"] for r in dao.mounts(account)
            if r["node"] == holder["node"] and r["job"] == holder["job"]}


def chat_bound(dao, account_cfg, holder: dict | None, level: str) -> set[str] | None:  # noqa: ANN001
    """The chats of ``account_cfg`` that a caller at ``level`` may read and
    answer, or ``None`` for no bound.

    A caller below trusted reaches only the chats it holds (channels.md R125
    item 2). A trusted caller is bounded only while it holds seats and none of
    them is trusted, so a lab seat never reads Tony's DM (users.md R126 item 2).
    """
    if account_cfg is None:
        return None if level == "trusted" else set()
    held = held_chats(dao, account_cfg.name, holder)
    if level != "trusted":
        return held
    if not held or any(seat_trust(account_cfg, c) == "trusted" for c in held):
        return None
    return held


# -- the pushed line ---------------------------------------------------------

def push_line(m: InboundMessage, level: str = "untrusted") -> str:
    """``<platform> [<level>] from <sender>[ [bot]][ ↳<thread>] [channel=<id> message_id=<id>]: <text>``
    on one line. The level is the seat's (R70). The ids are what the holder
    passes to ``social react``."""
    who = m.sender_name or m.sender_id or "?"
    if m.sender_bot:
        who += " [bot]"
    thread = (f" ↳{m.thread_id}"
              if m.thread_id and m.thread_id != m.message_id else "")
    text = " ⏎ ".join(line for line in (m.text or "").splitlines())
    if len(text) > TEXT_MAX:
        text = text[:TEXT_MAX] + " …(cut, full text in social history)"
    if m.attachments:
        n = len(m.attachments)
        text = f"{text} [+{n} file{'s' if n > 1 else ''}]".lstrip()
    ids = (f" [channel={m.channel_id} message_id={m.message_id}]"
           if m.message_id else "")
    return f"{m.platform} [{level}] from {who}{thread}{ids}: {text}"


# -- releasing dead holders --------------------------------------------------

async def prune(dao, list_jobs=live_jobs) -> list[dict]:  # noqa: ANN001
    """Release every seat whose holder's job is gone; returns the freed rows."""
    freed: list[dict] = []
    by_node: dict[str, set[str]] = {}
    for row in dao.mounts():
        by_node.setdefault(row["node"], set()).add(row["job"])
    for node, jobs in by_node.items():
        live = await list_jobs(node)
        if live is None:
            continue
        for job in sorted(jobs - live):
            rows = dao.release_holder(node, job)
            for row in rows:
                log.info("released seat %s/%s: holder %s is gone",
                         row["account"], row["channel_id"], holder_label(row))
            freed.extend(rows)
    return freed


async def prune_loop(dao, every_s: float = PRUNE_S) -> None:  # noqa: ANN001
    while True:
        await asyncio.sleep(every_s)
        try:
            await prune(dao)
        except Exception as exc:  # noqa: BLE001 — retried next round
            log.warning("seat prune failed: %s", exc)
