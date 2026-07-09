"""Connector registry + factory.

``REGISTRY`` maps a platform name to its :class:`~awm.social.connectors.base.Connector`
implementation. Adding a platform is one new module + one line here.
"""

from __future__ import annotations

from awm.social.config import AccountConfig
from awm.social.connectors.base import (
    Account, Attachment, Channel, Command, Connector, Identity, InboundMessage,
    OnCommand, OnMessage,
)
from awm.social.connectors.discord_conn import DiscordConnector
from awm.social.connectors.gmail_conn import GmailConnector
from awm.social.connectors.mira_conn import MiraConnector
from awm.social.connectors.slack_conn import SlackConnector

REGISTRY: dict[str, type[Connector]] = {
    "discord": DiscordConnector,
    "slack": SlackConnector,
    "gmail": GmailConnector,
}


def build(
    cfg: AccountConfig,
    on_message: OnMessage,
    on_command: OnCommand | None = None,
) -> Connector:
    """Construct the connector for one configured account.

    Routing: ``source = "mira"`` accounts (Slack/Teams driven on the mira host)
    use :class:`MiraConnector` regardless of platform; everything else uses the
    native per-platform connector from ``REGISTRY``. Config validation already
    rejects unknown platforms, so the ``ValueError`` is belt-and-braces.

    ``on_command`` is only honoured by connectors with a command surface
    (Discord); the others accept and ignore it. The mira connector has no
    command surface, so it is built with ``on_message`` only.
    """
    account = Account(
        name=cfg.name,
        platform=cfg.platform,
        token=cfg.token,
        app_token=cfg.app_token,
        cookie=cfg.cookie,
        creds_cmd=cfg.creds_cmd,
        display_name=cfg.display_name,
        address=cfg.address,
        source=cfg.source,
        mira_url=cfg.mira_url,
        mira_token=cfg.mira_token,
        mira_verify_tls=cfg.mira_verify_tls,
    )
    if cfg.source == "mira":
        return MiraConnector(account, on_message)
    cls = REGISTRY.get(cfg.platform)
    if cls is None:
        raise ValueError(f"no connector for platform {cfg.platform!r}")
    return cls(account, on_message, on_command=on_command)


__all__ = [
    "REGISTRY", "build", "Connector", "Account", "Attachment", "Channel",
    "Command", "Identity", "InboundMessage", "OnCommand", "OnMessage",
]
