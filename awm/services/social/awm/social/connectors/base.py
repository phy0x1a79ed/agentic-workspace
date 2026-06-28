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
    display_name: str | None = None
    address: str | None = None  # mailbox/login address (gmail uses this)


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


class Connector(ABC):
    """One live platform connection for one account.

    Implementations own their own reconnect/backoff inside :meth:`start`: a
    dropped platform socket must never kill the task or bubble out and stall the
    adapter's control-WS loop.
    """

    platform: str = "base"

    def __init__(self, account: Account, on_message: OnMessage) -> None:
        self.account = account
        self.on_message = on_message

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

    @abstractmethod
    async def list_channels(self) -> list[Channel]:
        """List channels/conversations visible to this account."""

    @abstractmethod
    async def identity(self) -> Identity:
        """Who this connector is logged in as."""

    @abstractmethod
    async def close(self) -> None:
        """Tear down the live connection."""
