"""What Tony and a watching session are told about a card, and how a Discord message goes out.

Card titles, bodies and agent names come from other parties. Tony reads the
direct messages as data. A session is told only a card's id, an event and who
acted: never a body, and a message's text never leaves the board.
"""

from __future__ import annotations

from typing import Any

from awm import gatewayclient

from awm.reporting import config

EXCERPT_CHARS = 300
REF_CHARS = 8

#: The only message texts the door classifies, because the door writes them
#: itself when a claimed task's plan moves.
PLAN_SUBMITTED = "plan submitted"
PLAN_APPROVED = "plan approved"


def short_ref(card_id: str) -> str:
    return card_id[:REF_CHARS]


def card_state(card: dict) -> str:
    """The card's state, from the previous release's status when the board sent no state."""
    state = card.get("state")
    if state:
        return str(state)
    return {"posted": "proposed", "in_progress": "planned", "done": "done",
            "failed": "rejected"}.get(str(card.get("status")), "proposed")


def swarm_of(party: Any) -> str:
    return str(party.get("swarm") or "") if isinstance(party, dict) else str(party or "")


def party_label(party: Any, agent: str | None = None) -> str:
    """``<swarm>/<principal>``, with the agent when the board stamped one."""
    if isinstance(party, dict):
        text = "/".join(p for p in (party.get("swarm"), party.get("principal")) if p) or "?"
    else:
        text = str(party or "?")
    return f"{text} ({agent})" if agent else text


def card_url(card: dict) -> str | None:
    base = config.trilium_url()
    note = card.get("note_id")
    return f"{base}/#root/{note}" if base and note else None


def excerpt(body: str | None, limit: int = EXCERPT_CHARS) -> str:
    text = " ".join(str(body or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def card_dm(card: dict, *, claimable: bool) -> str:
    """The message that tells Tony about a card, ending in the line he hands an agent."""
    ref = short_ref(card["id"])
    lines = [f"New post from {party_label(card.get('sender'), card.get('agent'))}"
             f" to {card.get('recipient') or '?'}",
             f"**{card.get('title') or '(no title)'}**"
             + (" [message]" if card.get("kind") == "message" else "")
             + (" (urgent)" if card.get("priority") == "urgent" else "")]
    if card.get("project"):
        lines.append(f"Project: {card['project']}")
    text = excerpt(card.get("body"))
    if text:
        lines += ["", text]
    url = card_url(card)
    lines.append("")
    if claimable:
        lines.append(f"claim task {ref}" + (f" {url}" if url else ""))
    else:
        lines.append(f"ref {ref}" + (f" {url}" if url else ""))
    return "\n".join(lines)


def wake_text(card_id: str, message: dict) -> str:
    """The line typed into the session a card is assigned to."""
    who = party_label(message.get("sender"), message.get("agent"))
    return f"New message on board card {card_id} from {who}, run operations board_messages"


def event_line(type_: str, card: dict) -> str | None:
    """The line typed into a watching session for one card event. None for an event it ignores."""
    head = f"Board card {card['id']}"
    if type_ == "card.claimed":
        return f"{head} claimed by {card.get('owner') or card.get('claimant') or '?'}"
    if type_ == "card.moved":
        return f"{head} moved to {card_state(card)}"
    if type_ == "card.completed":
        return f"{head} is done"
    if type_ == "card.failed":
        return f"{head} is rejected"
    if type_ == "card.message":
        message = card.get("message") or {}
        who = party_label(message.get("sender"), message.get("agent"))
        body = str(message.get("body") or "").strip().lower()
        if body == PLAN_SUBMITTED:
            return f"{head} plan submitted by {who}"
        if body == PLAN_APPROVED:
            return f"{head} plan approved by {who}"
        return f"{head} has a new message from {who}, run operations board_messages"
    return None


async def send_social(account: str, channel: str, text: str, *, ack_ref: str = "") -> None:
    args = {"account": account, "channel": channel, "text": text}
    if ack_ref:
        args["ack_ref"] = ack_ref
    result = await gatewayclient.call_maybe_peer(
        gatewayclient.peer_env("AWM_SOCIAL_PEER"), "social", "send", args)
    if isinstance(result, dict) and result.get("ok") is False:
        raise RuntimeError(f"social send refused: {result.get('error') or result}")


async def send_dm(text: str) -> None:
    """Send Tony a direct message."""
    account, channel = config.dm_target()
    await send_social(account, channel, text)
