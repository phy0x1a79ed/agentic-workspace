"""Hub adapter for the social service.

Boots the social service as a gateway-registered process: stands up its own DB,
loads the named accounts from ``social.toml``, opens a live connector per
account, and runs the shared :class:`awm.gatewayclient.ServiceAdapter` loop
(register → ready → serve → reconnect). The functions below are exposed over
the control WS and projected into the gateway catalog as ``social_*`` tools.

Inbound messages flow: connector → ``_on_message`` → **persist** (``social_messages``)
→ **emit** on ``/svc/social/emit/message``. Persist happens before emit so
receive + poll work even when no live subscriber is attached (``emit`` no-ops
when the control WS is down).

Run via ``run.sh`` (which the hub spawns and respawns):
    python -m awm.social.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm import config
from awm.gatewayclient import ServiceAdapter
from awm.social import config as social_config
from awm.social import connectors
from awm.social.dao import SocialDAO, init as dao_init

log = logging.getLogger("awm.social.hub_adapter")

# Populated by ``_on_start``: the live ServiceAdapter (so ``_on_message`` can
# reach ``emit``), the account name → Connector registry, and account name →
# AccountConfig (for the accounts listing + live-status).
_adapter: ServiceAdapter | None = None
_connectors: dict[str, connectors.Connector] = {}
_accounts: dict[str, social_config.AccountConfig] = {}
_dao = SocialDAO()


# -- inbound: persist + emit ------------------------------------------------

async def _on_message(inbound: connectors.InboundMessage) -> None:
    """Persist one inbound message, then emit it for live subscribers."""
    awm_user = _dao.lookup(inbound.platform, inbound.sender_id) or ""
    row = _dao.record_message(
        account=inbound.account,
        platform=inbound.platform,
        direction="in",
        channel_id=inbound.channel_id,
        channel_name=inbound.channel_name,
        thread_id=inbound.thread_id,
        sender_id=inbound.sender_id,
        sender_name=inbound.sender_name,
        awm_user=awm_user,
        text=inbound.text,
        ts=inbound.ts,
        message_id=inbound.message_id,
    )
    if row is None:
        return  # deduped redelivery — already persisted + emitted
    if _adapter is not None:
        await _adapter.emit("message", row)


# -- handlers ---------------------------------------------------------------

async def _h_send(args: dict) -> dict:
    account = args["account"]
    conn = _connectors.get(account)
    if conn is None:
        raise RuntimeError(
            f"account {account!r} is not configured or not connected")
    cfg = _accounts[account]
    sent = await conn.send(
        args["channel"], args["text"], thread=args.get("thread"))
    _dao.record_message(
        account=account,
        platform=cfg.platform,
        direction="out",
        channel_id=sent.get("channel_id", args["channel"]),
        thread_id=args.get("thread") or "",
        text=args["text"],
        ts=sent.get("ts", ""),
        message_id=sent.get("message_id", ""),
    )
    return {"ok": True, **sent}


async def _h_channels(args: dict) -> dict:
    account = args["account"]
    conn = _connectors.get(account)
    if conn is None:
        raise RuntimeError(
            f"account {account!r} is not configured or not connected")
    chans = await conn.list_channels()
    return {"channels": [vars(c) for c in chans]}


def _h_accounts(args: dict) -> dict:
    """Config + live status for every configured account."""
    out = []
    for name, cfg in _accounts.items():
        out.append({
            "name": name,
            "platform": cfg.platform,
            "kind": cfg.kind,
            "display_name": cfg.display_name or "",
            "enabled": cfg.enabled,
            "live": name in _connectors,
        })
    return {"accounts": out}


def _h_messages(args: dict) -> dict:
    return {"messages": _dao.list_messages(
        account=args.get("account"),
        platform=args.get("platform"),
        channel=args.get("channel"),
        since=args.get("since"),
        limit=args.get("limit", 50),
    )}


def _h_list_operators(args: dict) -> dict:
    return {"operators": _dao.list_operators(args.get("platform"))}


def _h_add_operator(args: dict) -> dict:
    return _dao.add_operator(
        args["platform"], args["platform_user_id"], args["awm_user"])


def _h_remove_operator(args: dict) -> dict:
    return {"removed": _dao.remove_operator(
        args["platform"], args["platform_user_id"])}


def _h_lookup(args: dict) -> dict:
    return {"awm_user": _dao.lookup(
        args["platform"], args["platform_user_id"])}


API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "send",
            "tool": "social_send",
            "description": "Send a message as a configured account to a channel.",
            "params": [
                {"name": "account", "type": "string", "required": True},
                {"name": "channel", "type": "string", "required": True},
                {"name": "text", "type": "string", "required": True},
                {"name": "thread", "type": "string", "required": False},
            ],
        },
        {
            "name": "messages",
            "tool": "social_messages",
            "description": "Poll stored messages (in + out), newest window first.",
            "params": [
                {"name": "account", "type": "string", "required": False},
                {"name": "platform", "type": "string", "required": False},
                {"name": "channel", "type": "string", "required": False},
                {"name": "since", "type": "string", "required": False},
                {"name": "limit", "type": "number", "required": False},
            ],
        },
        {
            "name": "accounts",
            "tool": "social_accounts",
            "description": "List configured accounts with platform, kind, and live status.",
        },
        {
            "name": "channels",
            "tool": "social_channels",
            "description": "List channels visible to a configured account.",
            "params": [
                {"name": "account", "type": "string", "required": True},
            ],
        },
        {
            "name": "list_operators",
            "tool": "social_operators",
            "description": "List whitelisted operators, optionally by platform.",
            "params": [
                {"name": "platform", "type": "string", "required": False},
            ],
        },
        {
            "name": "add_operator",
            "tool": "social_operator_add",
            "description": "Whitelist a platform user and map them to an awm_user.",
            "params": [
                {"name": "platform", "type": "string", "required": True},
                {"name": "platform_user_id", "type": "string", "required": True},
                {"name": "awm_user", "type": "string", "required": True},
            ],
        },
        {
            "name": "remove_operator",
            "tool": "social_operator_remove",
            "description": "Remove a platform user from the whitelist.",
            "params": [
                {"name": "platform", "type": "string", "required": True},
                {"name": "platform_user_id", "type": "string", "required": True},
            ],
        },
        {
            "name": "lookup",
            "tool": "social_lookup",
            "description": "Resolve a (platform, user id) to its awm_user (or null).",
            "params": [
                {"name": "platform", "type": "string", "required": True},
                {"name": "platform_user_id", "type": "string", "required": True},
            ],
        },
    ],
    "emitters": [{"topic": "message"}],
    "sessions": [],
}

HANDLERS = {
    "send": _h_send,
    "channels": _h_channels,
    "accounts": _h_accounts,
    "messages": _h_messages,
    "list_operators": _h_list_operators,
    "add_operator": _h_add_operator,
    "remove_operator": _h_remove_operator,
    "lookup": _h_lookup,
}


# -- startup ----------------------------------------------------------------

def _on_start() -> None:
    """Stand up the DB + boot a connector task per configured account.

    Sync (the adapter awaits it before the first connect). Connector tasks are
    scheduled on the running loop via ``create_task``; each owns its own
    reconnect so a dead platform never stalls the control-WS loop. A malformed
    ``social.toml`` is logged and skipped — the service still serves its DB-only
    tools (operators, message poll) with zero live connections.
    """
    config.load_env_file()
    dao_init()
    try:
        accounts = social_config.load()
    except social_config.SocialConfigError as exc:
        log.error("social.toml invalid; no accounts loaded: %s", exc)
        accounts = []

    loop = asyncio.get_event_loop()
    for cfg in accounts:
        _accounts[cfg.name] = cfg
        _dao.upsert_account(
            cfg.name, cfg.platform, kind=cfg.kind,
            display_name=cfg.display_name or "", enabled=cfg.enabled)
        if not cfg.enabled:
            log.info("account %s disabled; not connecting", cfg.name)
            continue
        try:
            conn = connectors.build(cfg, _on_message)
        except ValueError as exc:
            log.error("cannot build connector for %s: %s", cfg.name, exc)
            continue
        _connectors[cfg.name] = conn
        loop.create_task(conn.start())
        log.info("account %s (%s) connector launched",
                 cfg.name, cfg.platform)


async def main() -> None:
    global _adapter
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _adapter = ServiceAdapter(
        "social", API_MANIFEST, HANDLERS, on_start=_on_start,
    )
    await _adapter.run()


if __name__ == "__main__":
    asyncio.run(main())
