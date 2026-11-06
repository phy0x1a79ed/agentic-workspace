"""Where a message for this node's operator goes."""

from __future__ import annotations

import os

OPERATOR_DM_ACCOUNT_ENV = "AWM_OPERATOR_DM_ACCOUNT"
OPERATOR_DM_CHANNEL_ENV = "AWM_OPERATOR_DM_CHANNEL"
DEFAULT_OPERATOR_DM_ACCOUNT = "discord-bot"
#: Tony's DM with the bot, the channel when no setting names one.
DEFAULT_SWARM_OPERATOR_DM = "dm:188743359983124480"


def operator_dm() -> tuple[str, str]:
    """The social account and channel that reach this node's operator.

    ``AWM_OPERATOR_DM_CHANNEL`` names the channel; unset, it is Tony's DM.
    """
    account = (os.environ.get(OPERATOR_DM_ACCOUNT_ENV) or "").strip() or DEFAULT_OPERATOR_DM_ACCOUNT
    channel = (os.environ.get(OPERATOR_DM_CHANNEL_ENV) or "").strip()
    return account, channel or DEFAULT_SWARM_OPERATOR_DM
