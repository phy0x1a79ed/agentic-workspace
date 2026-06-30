"""Hub adapter for the social service.

Boots the social service as a gateway-registered process: stands up its own DB,
loads the named accounts from ``social.toml``, opens a live connector per
account, and runs the shared :class:`awm.gatewayclient.ServiceAdapter` loop
(register → ready → serve → reconnect). The functions below are exposed over
the control WS and projected into the gateway catalog as ``social_*`` tools.

Inbound messages flow: connector → ``_on_message`` → **emit** on
``/svc/social/emit/message``. Nothing is persisted — the external platforms are
the source of truth, and ``fetch`` / ``search`` / ``download_attachments`` query
them live. A small bounded in-memory set dedupes redelivered events (replacing
the former DB unique index). ``emit`` no-ops when no live subscriber is attached.

Run via ``run.sh`` (which the hub spawns and respawns):
    python -m awm.social.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from awm import config
from awm.gatewayclient import ServiceAdapter
from awm.social import config as social_config
from awm.social import connectors
from awm.social.attachments import write_attachments
from awm.social.dao import SocialDAO, init as dao_init

log = logging.getLogger("awm.social.hub_adapter")

# Populated by ``_on_start``: the live ServiceAdapter (so ``_on_message`` can
# reach ``emit``), the account name → Connector registry, and account name →
# AccountConfig (for the accounts listing + live-status).
_adapter: ServiceAdapter | None = None
_connectors: dict[str, connectors.Connector] = {}
_accounts: dict[str, social_config.AccountConfig] = {}
_dao = SocialDAO()

# Bounded in-memory dedupe of redelivered inbound events, keyed by
# (platform, account, message_id) — replaces the old DB unique index now that
# nothing is persisted. Cleared wholesale when it grows past the cap (redelivery
# dedupe is best-effort, so a rare re-emit after a clear is acceptable).
_seen: set[tuple[str, str, str]] = set()
_SEEN_MAX = 4096


def _conn(account: str) -> connectors.Connector:
    conn = _connectors.get(account)
    if conn is None:
        raise RuntimeError(
            f"account {account!r} is not configured or not connected")
    return conn


def _att_to_dict(a: connectors.Attachment) -> dict:
    """Public attachment metadata (the internal ``ref`` stays hidden — download
    re-resolves it live)."""
    return {"idx": a.idx, "filename": a.filename, "mime": a.mime,
            "size": a.size, "url": a.url}


def _msg_to_dict(m: connectors.InboundMessage, awm_user: str = "") -> dict:
    """Serialise an :class:`InboundMessage` for the emit/fetch/search surface."""
    return {
        "account": m.account,
        "platform": m.platform,
        "direction": "in",
        "channel_id": m.channel_id,
        "channel_name": m.channel_name,
        "thread_id": m.thread_id,
        "sender_id": m.sender_id,
        "sender_name": m.sender_name,
        "awm_user": awm_user,
        "text": m.text,
        "ts": m.ts,
        "message_id": m.message_id,
        "attachments": [_att_to_dict(a) for a in m.attachments],
    }


# -- inbound: emit (no persistence) -----------------------------------------

async def _on_message(inbound: connectors.InboundMessage) -> None:
    """Emit one inbound message for live subscribers (nothing is stored)."""
    key = (inbound.platform, inbound.account, inbound.message_id)
    if inbound.message_id:
        if key in _seen:
            return  # deduped redelivery
        if len(_seen) >= _SEEN_MAX:
            _seen.clear()
        _seen.add(key)
    if _adapter is not None:
        awm_user = _dao.lookup(inbound.platform, inbound.sender_id) or ""
        await _adapter.emit("message", _msg_to_dict(inbound, awm_user))


async def _on_command(cmd: connectors.Command) -> None:
    """Emit a normalised slash-command invocation for live subscribers.

    Unlike messages these aren't persisted — a command is an ephemeral trigger,
    not a record. The 2fa service subscribes to ``/svc/social/emit/command`` and
    reacts to ``name == "approve"`` by arming a Duo approval burst.
    """
    if _adapter is None:
        return
    payload = {
        "command": cmd.name,
        "account": cmd.account,
        "platform": cmd.platform,
        "options": cmd.options,
        "device": cmd.options.get("device"),
        "user_id": cmd.user_id,
        "user_name": cmd.user_name,
        "channel_id": cmd.channel_id,
        "is_dm": cmd.is_dm,
    }
    await _adapter.emit("command", payload)


# -- handlers ---------------------------------------------------------------

async def _h_send(args: dict) -> dict:
    # Send only — outbound messages are no longer mirrored to a local store.
    sent = await _conn(args["account"]).send(
        args["channel"], args["text"], thread=args.get("thread"))
    return {"ok": True, **sent}


async def _h_channels(args: dict) -> dict:
    chans = await _conn(args["account"]).list_channels(
        include_dms=bool(args.get("include_dms")))
    return {"channels": [vars(c) for c in chans]}


async def _h_open_dm(args: dict) -> dict:
    """Resolve a platform user (id, or name where supported) to a DM channel and
    open it. Returns the channel so it can be fed straight to ``fetch``/``send``."""
    ch = await _conn(args["account"]).open_dm(args["user"])
    return {"channel": vars(ch)}


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


async def _h_fetch(args: dict) -> dict:
    """Fetch a channel's existing messages live from the platform.

    Queries the connector directly (no local mirror) for messages that already
    exist, including ones sent before the service started. ``before`` pages
    backwards. Each returned message carries its ``attachments`` metadata.
    """
    conn = _conn(args["account"])
    msgs = await conn.fetch(
        args["channel"],
        limit=int(args.get("limit", 50)),
        before=args.get("before") or None,
    )
    rows = [_msg_to_dict(m, _dao.lookup(m.platform, m.sender_id) or "")
            for m in msgs]
    return {"messages": rows, "count": len(rows)}


async def _h_search(args: dict) -> dict:
    """Search a platform's own message index live (Slack search.messages, Gmail
    X-GM-RAW, Teams best-effort). Returns matches with attachment metadata.

    Platforms with no usable search surface (e.g. a Discord bot token) surface a
    clean "not supported" error rather than a stale local result.
    """
    conn = _conn(args["account"])
    msgs = await conn.search(
        args["query"],
        limit=int(args.get("limit", 50)),
        channel=args.get("channel") or None,
    )
    rows = [_msg_to_dict(m, _dao.lookup(m.platform, m.sender_id) or "")
            for m in msgs]
    return {"messages": rows, "count": len(rows)}


async def _h_download_attachments(args: dict) -> dict:
    """Download one message's attachments to a system temp dir (outside AWM_DIR).

    Re-fetches the message live via its connector, pulls each file's bytes, and
    writes them to a fresh ``mkdtemp`` directory. Returns the absolute paths +
    metadata; ``idx`` optionally restricts to a single attachment.
    """
    conn = _conn(args["account"])
    idx_raw = args.get("idx")
    idx = int(idx_raw) if idx_raw not in (None, "") else None
    files = await conn.download_attachments(
        args["channel"], str(args["message_id"]), idx=idx)
    written = write_attachments(files)
    out_dir = os.path.dirname(written[0]["path"]) if written else ""
    return {"files": written, "count": len(written), "dir": out_dir}


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
            "name": "fetch",
            "tool": "social_fetch",
            "description": "Fetch a channel's existing messages live from the "
                           "platform (incl. ones from before the service started), "
                           "with attachment metadata. `before` pages backwards. "
                           "Nothing is stored — the platform is the source of truth.",
            "params": [
                {"name": "account", "type": "string", "required": True},
                {"name": "channel", "type": "string", "required": True},
                {"name": "limit", "type": "number", "required": False},
                {"name": "before", "type": "string", "required": False},
            ],
        },
        {
            "name": "search",
            "tool": "social_search",
            "description": "Search a platform's own message index live (Slack "
                           "search.messages, Gmail X-GM-RAW, Teams best-effort). "
                           "Returns matches with attachment metadata. Not supported "
                           "for Discord bot accounts (use fetch).",
            "params": [
                {"name": "account", "type": "string", "required": True},
                {"name": "query", "type": "string", "required": True},
                {"name": "channel", "type": "string", "required": False},
                {"name": "limit", "type": "number", "required": False},
            ],
        },
        {
            "name": "download_attachments",
            "tool": "social_download_attachments",
            "description": "Download a message's attachments to a system temp dir "
                           "(outside the awm workspace). Re-fetches the message "
                           "live, returns the absolute file paths + metadata. "
                           "`idx` optionally selects a single attachment.",
            "timeout": 300,
            "params": [
                {"name": "account", "type": "string", "required": True},
                {"name": "channel", "type": "string", "required": True},
                {"name": "message_id", "type": "string", "required": True},
                {"name": "idx", "type": "number", "required": False},
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
            "description": "List channels visible to a configured account. "
                           "`include_dms=true` also enumerates direct/group DMs "
                           "(kind 'dm'/'group') where the platform supports it.",
            "params": [
                {"name": "account", "type": "string", "required": True},
                {"name": "include_dms", "type": "boolean", "required": False},
            ],
        },
        {
            "name": "open_dm",
            "tool": "social_open_dm",
            "description": "Resolve a platform user (by id, or by name where the "
                           "platform supports it) to a direct-message channel and "
                           "open it. Returns the channel for use with fetch/send.",
            "params": [
                {"name": "account", "type": "string", "required": True},
                {"name": "user", "type": "string", "required": True},
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
    "emitters": [{"topic": "message"}, {"topic": "command"}],
    "sessions": [],
}

HANDLERS = {
    "send": _h_send,
    "channels": _h_channels,
    "open_dm": _h_open_dm,
    "accounts": _h_accounts,
    "fetch": _h_fetch,
    "search": _h_search,
    "download_attachments": _h_download_attachments,
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
    tools (the operator allowlist) with zero live connections.
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
            conn = connectors.build(cfg, _on_message, _on_command)
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
