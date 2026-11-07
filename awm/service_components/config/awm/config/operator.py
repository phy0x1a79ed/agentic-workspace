"""Where a message for this node's operator goes."""

from __future__ import annotations

import os

OPERATOR_DM_ACCOUNT_ENV = "AWM_OPERATOR_DM_ACCOUNT"
OPERATOR_DM_CHANNEL_ENV = "AWM_OPERATOR_DM_CHANNEL"
DEFAULT_OPERATOR_DM_ACCOUNT = "discord-bot"
#: unimatrix0#notifications, the channel when no setting names one. Tony's DM with
#: the bot stays free for conversation.
DEFAULT_SWARM_OPERATOR_DM = "1522674357762261112"


def operator_dm() -> tuple[str, str]:
    """The social account and channel that reach this node's operator.

    ``AWM_OPERATOR_DM_CHANNEL`` names the channel; unset, it is unimatrix0#notifications.
    """
    account = (os.environ.get(OPERATOR_DM_ACCOUNT_ENV) or "").strip() or DEFAULT_OPERATOR_DM_ACCOUNT
    channel = (os.environ.get(OPERATOR_DM_CHANNEL_ENV) or "").strip()
    return account, channel or DEFAULT_SWARM_OPERATOR_DM
