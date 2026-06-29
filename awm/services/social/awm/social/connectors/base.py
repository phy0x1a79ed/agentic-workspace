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

import logging
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
class InboundMessage:
    """A message received from a platform, normalised across connectors.

    ``message_id`` is the platform's own id (used for redelivery dedupe);
    ``ts`` is the platform timestamp string when available.
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
    raw: dict = field(default_factory=dict, compare=False)


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


class Connector(ABC):
    """One live platform connection for one account.

    Implementations own their own reconnect/backoff inside :meth:`start`: a
    dropped platform socket must never kill the task or bubble out and stall the
    adapter's control-WS loop.
    """

    platform: str = "base"

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

    async def history(
        self, channel: str, *, limit: int = 50, before: str | None = None
    ) -> list[InboundMessage]:
        """Fetch existing messages from ``channel`` on demand (newest batch).

        Unlike :meth:`start` — which only live-tails messages arriving *after*
        connect — ``history`` pulls messages that already exist on the platform,
        including ones sent before the service came up. ``before`` is an optional
        platform cursor (message id / ts) to page backwards from. Returns
        normalised :class:`InboundMessage` objects ordered **oldest→newest** (the
        same order :func:`SocialDAO.list_messages` returns), so the adapter can
        persist them straight through its dedupe path.

        Non-abstract: a connector whose platform can't fetch history simply leaves
        this default, which raises and the verb surfaces a clean error.
        """
        raise NotImplementedError(
            f"{self.platform} connector does not support history fetch")

    @abstractmethod
    async def list_channels(self, *, include_dms: bool = False) -> list[Channel]:
        """List channels/conversations visible to this account.

        ``include_dms=True`` additionally enumerates direct/group DMs (their
        ``kind`` is ``"dm"`` / ``"group"``). Connectors whose platform has no DM
        concept (or that can't enumerate them) ignore the flag and return their
        channel list unchanged.
        """

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
