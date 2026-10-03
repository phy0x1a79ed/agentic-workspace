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
from awm.social import buckets as buckets_mod
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
# Cloud file-store backends, keyed by bucket name. Plain request/response
# objects (no live socket), so unlike connectors they get no background task.
_buckets: dict[str, buckets_mod.Bucket] = {}
_bucket_cfgs: dict[str, social_config.BucketConfig] = {}
_dao = SocialDAO()

# Bounded in-memory dedupe of redelivered inbound events, keyed by
# (platform, account, message_id) — replaces the old DB unique index now that
# nothing is persisted. Cleared wholesale when it grows past the cap (redelivery
# dedupe is best-effort, so a rare re-emit after a clear is acceptable).
_seen: set[tuple[str, str, str]] = set()
_SEEN_MAX = 4096


# ---------------------------------------------------------------------------
# Fleet singletons — an account that is one session for the whole fleet lives on
# exactly one node. The selector below is the single branch point (the same shape
# gatewayclient uses for service-level singletons): unset ⇒ this node OWNS the
# singletons and connects them; set ⇒ this node BORROWS them, connects none of
# them, and forwards any verb naming one to the owner. Read fresh per use so a
# re-home takes effect without a restart.
# ---------------------------------------------------------------------------

_SOCIAL_PEER_ENV = "AWM_SOCIAL_PEER"


def _is_borrower() -> bool:
    """True when this node borrows social's singletons from a peer."""
    from awm import gatewayclient

    return gatewayclient.peer_env(_SOCIAL_PEER_ENV) is not None


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


# The command name a self-test probe carries. Deliberately NOT a registered
# slash command, so no operator can ever produce one, and no consumer's
# domain handler will match it.
PROBE_COMMAND = "__awm_probe__"


async def _h_emit_probe(args: dict) -> dict:
    """Emit a synthetic ``command`` event carrying only a nonce.

    Lets a subscriber prove on demand that it can still *receive* — the one
    thing no existing check covers, and the gap that let ssh sit deaf through
    three ``/approve`` attempts on 2026-07-26.

    The payload is deliberately inert: no device, no account, no channel_id.
    It therefore cannot open an approval window, cannot arm a Duo burst, and
    cannot generate any Discord traffic. The command name is a constant that
    is not a registered slash command.
    """
    if _adapter is None:
        return {"ok": False, "error": "social adapter not started"}
    nonce = str(args.get("nonce") or "")
    if not nonce:
        return {"ok": False, "error": "nonce is required"}
    await _adapter.emit("command", {"command": PROBE_COMMAND, "nonce": nonce})
    return {"ok": True, "nonce": nonce}


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


async def _h_accounts(args: dict) -> dict:
    """Config + live status for every account reachable from this node.

    Async because on a borrowing node it merges the owner's accounts in over the
    network. That merge is not cosmetic: a singleton is deliberately absent from
    this node's connectors, so without it a caller inspects the list, does not
    see the account, and never attempts the call that would have worked.

    Best-effort — if the peer is unreachable the local accounts are still
    returned, with ``peer_error`` naming why the rest are missing, rather than
    failing the whole listing.
    """
    out = []
    for name, cfg in _accounts.items():
        out.append({
            "name": name,
            "platform": cfg.platform,
            "kind": cfg.kind,
            "display_name": cfg.display_name or "",
            "enabled": cfg.enabled,
            "live": name in _connectors,
            "singleton": cfg.singleton,
            "owner": "local",
        })

    from awm import gatewayclient

    peer = gatewayclient.peer_env(_SOCIAL_PEER_ENV)
    if not peer:
        return {"accounts": out}

    by_name = {a["name"]: a for a in out}
    try:
        remote = await gatewayclient.call_peer(peer, "social", "accounts", {})
    except (gatewayclient.PeerError, gatewayclient.GatewayCallError) as exc:
        log.warning("could not list peer %s accounts: %s", peer, exc)
        return {"accounts": out, "peer": peer, "peer_error": str(exc)}

    for acc in (remote or {}).get("accounts", []):
        name = acc.get("name")
        mine = by_name.get(name)
        if mine is None:
            out.append({**acc, "owner": peer})
            continue
        if mine.get("singleton"):
            # Configured here but owned there. Saying "local" would be a lie in
            # the one case a caller most needs the truth: it is the session on
            # the *peer* that answers, so its liveness is the one that matters.
            mine["owner"] = peer
            mine["live"] = bool(acc.get("live"))
    return {"accounts": out, "peer": peer}


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


def _written_reply(written: list[dict]) -> dict:
    """The common shape both file-returning verbs answer with.

    ``path`` and ``dir`` are absolute on **this** node, so the reply also names
    which node that is: a caller that borrowed ``social`` from a peer needs that
    to know the paths are not its own, and the per-file ``url`` to do something
    about it (see ``attachments`` module docstring).
    """
    return {
        "files": written,
        "count": len(written),
        "dir": os.path.dirname(written[0]["path"]) if written else "",
        "node": config.node_name(),
        "edge_url": config.edge_url() or "",
    }


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
    return _written_reply(write_attachments(files))


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


# -- bucket (cloud file store) handlers -------------------------------------

def _bucket(name: str) -> buckets_mod.Bucket:
    b = _buckets.get(name)
    if b is None:
        raise RuntimeError(
            f"bucket {name!r} is not configured or failed to build")
    return b


def _entry_to_dict(e: buckets_mod.Entry) -> dict:
    return {"name": e.name, "path": e.path, "is_dir": e.is_dir,
            "size": e.size, "mime": e.mime, "modified": e.modified, "id": e.id}


def _h_buckets(args: dict) -> dict:
    """List configured buckets with kind + live status."""
    out = []
    for name, cfg in _bucket_cfgs.items():
        out.append({
            "name": name,
            "kind": cfg.kind,
            "root": cfg.root,
            "live": name in _buckets,
        })
    return {"buckets": out}


async def _h_bucket_ls(args: dict) -> dict:
    entries = await _bucket(args["bucket"]).ls(args.get("path", "") or "")
    return {"entries": [_entry_to_dict(e) for e in entries],
            "count": len(entries)}


async def _h_bucket_get(args: dict) -> dict:
    """Download one file to a system temp dir (outside AWM_DIR) and return its
    path — reuses the attachment sink so large files never inline into the RPC."""
    filename, mime, data = await _bucket(args["bucket"]).get(args["path"])
    return _written_reply(write_attachments([(filename, mime, data)]))


async def _h_bucket_put(args: dict) -> dict:
    """Upload a local file's bytes to ``path`` in the bucket (create/overwrite).

    The source is a local filesystem path (``src``) the service reads — bytes are
    never inlined into the RPC payload.
    """
    src = args["src"]
    with open(src, "rb") as fh:
        data = fh.read()
    entry = await _bucket(args["bucket"]).put(
        args["path"], data, mime=args.get("mime") or None)
    return {"entry": _entry_to_dict(entry)}


async def _h_bucket_rm(args: dict) -> dict:
    await _bucket(args["bucket"]).rm(args["path"])
    return {"ok": True, "path": args["path"]}


async def _h_bucket_search(args: dict) -> dict:
    entries = await _bucket(args["bucket"]).search(
        args["query"], limit=int(args.get("limit", 50)))
    return {"entries": [_entry_to_dict(e) for e in entries],
            "count": len(entries)}


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
        {
            "name": "buckets",
            "tool": "social_buckets",
            "description": "List configured cloud file-store buckets (Google "
                           "Drive, OneDrive/SharePoint) with kind, root, and live "
                           "status.",
        },
        {
            "name": "bucket_ls",
            "tool": "social_bucket_ls",
            "description": "List the immediate children of a folder in a bucket. "
                           "`path` is relative to the bucket root (omit for root). "
                           "Returns entries with name, path, is_dir, size, mime.",
            "params": [
                {"name": "bucket", "type": "string", "required": True},
                {"name": "path", "type": "string", "required": False},
            ],
        },
        {
            "name": "bucket_get",
            "tool": "social_bucket_get",
            "description": "Download one file from a bucket to a system temp dir "
                           "(outside the awm workspace) and return its absolute "
                           "path + metadata. Google-native docs are exported "
                           "(Docs→pdf, Sheets→csv).",
            "timeout": 300,
            "params": [
                {"name": "bucket", "type": "string", "required": True},
                {"name": "path", "type": "string", "required": True},
            ],
        },
        {
            "name": "bucket_put",
            "tool": "social_bucket_put",
            "description": "Upload a local file (`src` = a filesystem path) to "
                           "`path` in a bucket, creating or overwriting it. "
                           "Returns the resulting entry.",
            "timeout": 300,
            "params": [
                {"name": "bucket", "type": "string", "required": True},
                {"name": "path", "type": "string", "required": True},
                {"name": "src", "type": "string", "required": True},
                {"name": "mime", "type": "string", "required": False},
            ],
        },
        {
            "name": "bucket_rm",
            "tool": "social_bucket_rm",
            "description": "Delete the file/folder at `path` in a bucket.",
            "params": [
                {"name": "bucket", "type": "string", "required": True},
                {"name": "path", "type": "string", "required": True},
            ],
        },
        {
            "name": "bucket_search",
            "tool": "social_bucket_search",
            "description": "Search a bucket for files matching `query` (Google "
                           "Drive: name match across the drive; OneDrive: "
                           "SharePoint search). Returns matching entries.",
            "params": [
                {"name": "bucket", "type": "string", "required": True},
                {"name": "query", "type": "string", "required": True},
                {"name": "limit", "type": "number", "required": False},
            ],
        },
        {
            "name": "emit_probe",
            "tool": "social_emit_probe",
            "description": "Emit a synthetic, inert `command` event carrying "
                           "only `nonce`, so a subscriber can prove on demand "
                           "that it can still receive. Opens no window, arms "
                           "nothing, sends no message.",
            "params": [
                {"name": "nonce", "type": "string", "required": True},
            ],
        },
    ],
    "emitters": [{"topic": "message"}, {"topic": "command"}],
    "sessions": [],
}

def _forwarding(fn: str, handler: Any) -> Any:
    """Wrap an account-taking handler so a borrowed account resolves on its owner.

    Applied to the six verbs that name an ``account`` — and only those. All six
    are already coroutine functions, so wrapping them changes nothing about how
    the adapter runs them; a blanket wrap over ``HANDLERS`` would instead turn
    the sync handlers into coroutine functions and move their DB work off the
    worker thread onto the event loop (see ``ServiceAdapter._dispatch``, which
    branches on ``inspect.iscoroutinefunction``). The wrapper keeps arity at one
    for the same reason — the adapter reads handler arity to decide whether to
    pass caller identity.

    The branch is on the *selector*, never on "is this account missing": the
    owner node leaves it unset, so a typo'd account there still fails locally
    with a useful error instead of being bounced to a peer that doesn't have it
    either. That also makes a forward loop impossible — the owner never forwards.
    """
    async def _wrapped(args: dict) -> Any:
        from awm import gatewayclient

        peer = gatewayclient.peer_env(_SOCIAL_PEER_ENV)
        account = (args or {}).get("account")
        if peer and account and account not in _connectors:
            try:
                return await gatewayclient.call_peer(peer, "social", fn, args)
            except gatewayclient.PeerError as exc:
                # Distinguish "couldn't reach the owner" from "the owner said
                # no" — they have completely different fixes, and a merged
                # message sends the next incident down the wrong path.
                raise RuntimeError(
                    f"account {account!r} is a fleet singleton owned by peer "
                    f"{peer!r}, which could not be reached: {exc}") from exc
        return await handler(args)

    _wrapped.__name__ = getattr(handler, "__name__", f"_h_{fn}")
    _wrapped.__doc__ = handler.__doc__
    return _wrapped


HANDLERS = {
    "send": _forwarding("send", _h_send),
    "channels": _forwarding("channels", _h_channels),
    "open_dm": _forwarding("open_dm", _h_open_dm),
    "accounts": _h_accounts,
    "fetch": _forwarding("fetch", _h_fetch),
    "search": _forwarding("search", _h_search),
    "download_attachments": _forwarding(
        "download_attachments", _h_download_attachments),
    "list_operators": _h_list_operators,
    "add_operator": _h_add_operator,
    "remove_operator": _h_remove_operator,
    "lookup": _h_lookup,
    "buckets": _h_buckets,
    "bucket_ls": _h_bucket_ls,
    "bucket_get": _h_bucket_get,
    "bucket_put": _h_bucket_put,
    "bucket_rm": _h_bucket_rm,
    "bucket_search": _h_bucket_search,
    "emit_probe": _h_emit_probe,
}


# -- startup ----------------------------------------------------------------

def _on_start() -> None:
    """Stand up the DB + boot a connector task per configured account.

    Sync (the adapter runs it alongside registration). Connector tasks are
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
    borrowed = _is_borrower()
    for cfg in accounts:
        _accounts[cfg.name] = cfg
        _dao.upsert_account(
            cfg.name, cfg.platform, kind=cfg.kind,
            display_name=cfg.display_name or "", enabled=cfg.enabled)
        if not cfg.enabled:
            log.info("account %s disabled; not connecting", cfg.name)
            continue
        if cfg.singleton and borrowed:
            # Owned by another node. Connecting it here would be a second
            # session on one identity, not a second client — which is exactly
            # how two live Discord bots on one token produce "Unknown
            # interaction". Verbs naming it forward to the owner instead.
            log.info("account %s is a fleet singleton owned by %s; "
                     "not connecting locally (calls forward to the peer)",
                     cfg.name, os.environ.get(_SOCIAL_PEER_ENV, "").strip())
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

    # Cloud file-store buckets are plain request/response objects — built eagerly
    # but with no background task. A malformed [bucket] section is logged and
    # skipped, fully isolated from the account path above.
    try:
        bucket_cfgs = social_config.load_buckets()
    except social_config.SocialConfigError as exc:
        log.error("social.toml [bucket] invalid; no buckets loaded: %s", exc)
        bucket_cfgs = []
    for bcfg in bucket_cfgs:
        _bucket_cfgs[bcfg.name] = bcfg
        try:
            _buckets[bcfg.name] = buckets_mod.build(bcfg)
        except (ValueError, RuntimeError) as exc:
            log.error("cannot build bucket %s: %s", bcfg.name, exc)
            continue
        log.info("bucket %s (%s) ready", bcfg.name, bcfg.kind)


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
