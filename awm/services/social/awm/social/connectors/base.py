"""Connector abstraction — one live platform connection per configured account.

A :class:`Connector` owns the socket to one platform for one account: it opens
the live connection, runs a receive loop (forwarding each inbound message to an
injected ``on_message`` callback), and sends outbound messages. Persistence and
hub-emit stay OUT of the connectors — the adapter injects ``on_message`` and
does the DB write + emit there, so a connector is a pure platform shim.

Adding a platform = one new module implementing this ABC + one line in
``connectors/__init__.REGISTRY``.
"""

from __future__ import annotations

import base64
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Awaitable, Callable

log = logging.getLogger("awm.social.connectors")


@dataclass(frozen=True)
class Account:
    """The identity a connector acts as — the config, minus nothing secret-y
    that the connector itself doesn't need. Passed to the connector ctor."""

    name: str
    platform: str
    token: str
    app_token: str | None = None
    cookie: str | None = None     # Slack session 'd' cookie (xoxc session mode)
    creds_cmd: str | None = None  # command printing {token,cookie} JSON (mira pull)
    display_name: str | None = None
    address: str | None = None  # mailbox/login address (gmail uses this)
    # mira-routed accounts (source="mira"): the connector is a thin client of the
    # mira API daemon — all platform logic runs there, next to the live session.
    source: str | None = None       # "mira" routes via MiraConnector
    mira_url: str | None = None     # e.g. https://172.16.0.24:7822
    mira_token: str | None = None   # bearer for the mira daemon
    mira_verify_tls: bool = False   # mira uses a self-signed cert by default


@dataclass(frozen=True)
class Attachment:
    """A file attached to a message, normalised across connectors.

    ``url`` and ``ref`` are *advisory* — a hint at where the bytes live and the
    opaque data a connector would need to re-resolve them. They are NOT a
    download contract: :meth:`Connector.download_attachments` re-fetches the
    message live and pulls the bytes itself (CDN urls expire, session tokens
    rotate), so a caller never has to round-trip ``ref``. ``size`` is the byte
    count when the platform reports it (``0`` when unknown).
    """

    idx: int = 0
    filename: str = ""
    mime: str = ""
    size: int = 0
    url: str = ""
    ref: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class InboundMessage:
    """A message received from a platform, normalised across connectors.

    ``message_id`` is the platform's own id (used for redelivery dedupe);
    ``ts`` is the platform timestamp string when available. ``sender_bot`` is
    True when the platform marks the sender as a bot. ``to_holder`` is False
    for a message the connector keeps from a seat's holder.
    """

    account: str
    platform: str
    channel_id: str
    text: str
    sender_id: str = ""
    sender_name: str = ""
    channel_name: str = ""
    thread_id: str = ""
    message_id: str = ""
    ts: str = ""
    attachments: list = field(default_factory=list, compare=False)
    raw: dict = field(default_factory=dict, compare=False)
    sender_bot: bool = False
    to_holder: bool = True


@dataclass(frozen=True)
class Command:
    """A slash/application-command invocation, normalised across connectors.

    Only platforms with a real command surface (Discord application commands)
    ever produce these; everything else leaves ``on_command`` unused. The
    adapter turns one of these into an ``emit`` so other services can react.
    """

    account: str
    platform: str
    name: str                                # command name, e.g. "approve"
    options: dict = field(default_factory=dict)  # parsed option name → value
    user_id: str = ""
    user_name: str = ""
    channel_id: str = ""
    is_dm: bool = False                       # invoked in a DM to the bot?
    operator: str = ""                        # awm_user the sender maps to


@dataclass(frozen=True)
class Channel:
    """A channel/conversation a connector can list."""

    id: str
    name: str = ""
    kind: str = ""  # e.g. "text", "dm", "channel", "group"


@dataclass(frozen=True)
class Identity:
    """Who the connector is logged in as on the platform."""

    id: str
    name: str = ""


OnMessage = Callable[[InboundMessage], Awaitable[None]]
OnCommand = Callable[[Command], Awaitable[None]]


def file_bytes(f: dict) -> bytes:
    """The content of one outbound attachment: inline ``text``, base64 ``b64``,
    or a ``path`` on this node."""
    if "path" in f:
        with open(f["path"], "rb") as fh:
            return fh.read()
    if "b64" in f:
        return base64.b64decode(f["b64"])
    return str(f.get("text", "")).encode()


def file_size(f: dict) -> int:
    if "path" in f:
        return os.path.getsize(f["path"])
    if "b64" in f:
        return len(f["b64"]) * 3 // 4 - f["b64"][-2:].count("=")
    return len(str(f.get("text", "")).encode())


async def react_by_message(
    conn: "Connector", channel: str, emoji: str, *,
    remove: bool, thread: str | None,
) -> dict:
    """Acknowledge with the emoji alone as a message, for a Slack app that
    lacks ``reactions:write``. Remove has nothing to take back, so it refuses."""
    from awm.social.emoji import to_char

    if remove:
        raise RuntimeError(
            f"{conn.platform} posts a reaction as a message until the app holds "
            "reactions:write, so there is no reaction to remove")
    char = to_char(emoji)
    if char is None:
        raise RuntimeError(
            f"{emoji!r} has no known character to post. Pass the emoji itself")
    sent = await conn.send(channel, char, thread=thread)
    return {"as": "message", "emoji": char, **sent}


class Connector(ABC):
    """One live platform connection for one account.

    Implementations own their own reconnect/backoff inside :meth:`start`: a
    dropped platform socket must never kill the task or bubble out and stall the
    adapter's control-WS loop.
    """

    platform: str = "base"
    # True for a connector that keeps a synced local history (a bot, which is
    # the only reader of its chats). The adapter then injects ``history``.
    keeps_history: bool = False
    history = None  # awm.social.history.History, set by the adapter
    # True for a connector whose ``send`` and ``edit_ack`` take ``files``: a list
    # of attachments, each ``{"filename"}`` plus one of ``text``, ``b64`` or
    # ``path`` (see :func:`file_bytes`).
    sends_files: bool = False
    # The most bytes one message may attach, all files together.
    max_file_bytes: int = 0

    def __init__(
        self,
        account: Account,
        on_message: OnMessage,
        on_command: OnCommand | None = None,
    ) -> None:
        self.account = account
        self.on_message = on_message
        # Only set for platforms with a command surface (Discord). Others ignore.
        self.on_command = on_command

    @abstractmethod
    async def start(self) -> None:
        """Open the live connection and run the receive loop until cancelled.

        Owns its own reconnect/backoff. Returns only on cancellation or a
        permanent, unrecoverable failure (e.g. missing dependency / bad token).
        """

    @abstractmethod
    async def send(
        self, channel: str, text: str, *, thread: str | None = None
    ) -> dict:
        """Send ``text`` to ``channel`` (optionally in ``thread``).

        Returns a small dict describing the sent message (at least
        ``message_id`` when the platform supplies one).
        """

    async def edit_ack(self, ref: str, text: str, *,
                       files: list[dict] | None = None) -> dict | None:
        """Rewrite the acknowledgement of the command carrying ``ack_ref=ref``.

        Returns ``None`` when the platform can no longer edit it (expired,
        unknown, or no command surface at all); the caller then sends anew.
        """
        return None

    async def fetch(
        self, channel: str, *, limit: int = 50, before: str | None = None
    ) -> list[InboundMessage]:
        """Fetch existing messages from ``channel`` live (newest batch).

        Unlike :meth:`start` — which only live-tails messages arriving *after*
        connect — ``fetch`` queries the platform directly for messages that
        already exist, including ones sent before the service came up. ``before``
        is an optional platform cursor (message id / ts) to page backwards from.
        Returns normalised :class:`InboundMessage` objects ordered
        **oldest→newest**, each with its :attr:`InboundMessage.attachments`
        populated where the platform exposes file metadata. Nothing is persisted
        — the platform is the source of truth.

        Non-abstract: a connector whose platform can't fetch messages simply
        leaves this default, which raises and the verb surfaces a clean error.
        """
        raise NotImplementedError(
            f"{self.platform} connector does not support fetch")

    async def search(
        self, query: str, *, limit: int = 50, channel: str | None = None
    ) -> list[InboundMessage]:
        """Search the platform's own message index for ``query``, live.

        Runs the platform's native search (Slack ``search.messages``, Gmail
        ``X-GM-RAW``, …) rather than a local mirror, so results reflect the
        account's full reachable history. ``channel`` optionally scopes the
        search to one conversation where the platform supports it. Returns
        normalised :class:`InboundMessage` objects (attachments populated),
        newest-first as the platform ranks them.

        Non-abstract: a platform with no usable search surface (e.g. a Discord
        *bot* token) leaves this default, which raises a clean error.
        """
        raise NotImplementedError(
            f"{self.platform} connector does not support search")

    async def download_attachments(
        self, channel: str, message_id: str, *, idx: int | None = None
    ) -> list[tuple[str, str, bytes]]:
        """Re-fetch one message live and return its attachments' bytes.

        Returns a list of ``(filename, mime, data)`` tuples — the connector
        re-resolves the message by ``message_id`` (urls/tokens are short-lived,
        never trusted from a prior fetch) and pulls each file in-session, so the
        call is self-contained. ``idx`` optionally restricts to a single
        attachment by its :attr:`Attachment.idx`. The adapter writes the bytes to
        a temp dir; the connector itself never touches disk.

        Non-abstract: a platform that can't retrieve files leaves this default,
        which raises a clean error.
        """
        raise NotImplementedError(
            f"{self.platform} connector does not support attachment download")

    async def react(
        self, channel: str, message_id: str, emoji: str, *,
        remove: bool = False, thread: str | None = None,
    ) -> dict:
        """Add ``emoji`` to message ``message_id`` in ``channel``, or take it off.

        ``emoji`` is a character, a name or ``:name:`` (see :mod:`awm.social.emoji`).
        ``thread`` is where a platform that answers with a message instead of a
        reaction posts it. Returns ``{"as": "reaction" | "message", …}``.

        Non-abstract: a platform without reactions leaves this default, which
        raises a clean error.
        """
        raise NotImplementedError(
            f"{self.platform} connector does not support reactions")

    @abstractmethod
    async def list_channels(self, *, include_dms: bool = False) -> list[Channel]:
        """List channels/conversations visible to this account.

        ``include_dms=True`` additionally enumerates direct/group DMs (their
        ``kind`` is ``"dm"`` / ``"group"``). Connectors whose platform has no DM
        concept (or that can't enumerate them) ignore the flag and return their
        channel list unchanged.
        """

    async def list_seats(self) -> list[Channel]:
        """The chats this account is in that a session may mount.

        The default is every listed channel and DM. A connector overrides it to
        list memberships only, or to leave out chats reserved for other uses.
        """
        return await self.list_channels(include_dms=True)

    async def open_dm(self, user: str) -> Channel:
        """Resolve a platform user (by id, or by name where supported) to a DM
        conversation and open it, returning the :class:`Channel`.

        Non-abstract: a connector whose platform has no DM concept leaves this
        default, which raises and the verb surfaces a clean error.
        """
        raise NotImplementedError(
            f"{self.platform} connector does not support opening DMs")

    @abstractmethod
    async def identity(self) -> Identity:
        """Who this connector is logged in as."""

    @abstractmethod
    async def close(self) -> None:
        """Tear down the live connection."""
