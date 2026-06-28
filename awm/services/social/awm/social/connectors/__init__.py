"""Connector registry + factory.

``REGISTRY`` maps a platform name to its :class:`~awm.social.connectors.base.Connector`
implementation. Adding a platform is one new module + one line here.
"""

from __future__ import annotations

from awm.social.config import AccountConfig
from awm.social.connectors.base import (
    Account, Channel, Connector, Identity, InboundMessage, OnMessage,
)
from awm.social.connectors.discord_conn import DiscordConnector
from awm.social.connectors.gmail_conn import GmailConnector
from awm.social.connectors.slack_conn import SlackConnector

REGISTRY: dict[str, type[Connector]] = {
    "discord": DiscordConnector,
    "slack": SlackConnector,
    "gmail": GmailConnector,
}


def build(cfg: AccountConfig, on_message: OnMessage) -> Connector:
    """Construct the connector for one configured account.

    Raises ``KeyError`` (via a clear ``ValueError``) for an unknown platform —
    config validation already rejects those, so this is a belt-and-braces guard.
    """
    cls = REGISTRY.get(cfg.platform)
    if cls is None:
        raise ValueError(f"no connector for platform {cfg.platform!r}")
    account = Account(
        name=cfg.name,
        platform=cfg.platform,
        token=cfg.token,
        app_token=cfg.app_token,
        cookie=cfg.cookie,
        creds_cmd=cfg.creds_cmd,
        display_name=cfg.display_name,
        address=cfg.address,
    )
    return cls(account, on_message)


__all__ = [
    "REGISTRY", "build", "Connector", "Account", "Channel", "Identity",
    "InboundMessage", "OnMessage",
]
