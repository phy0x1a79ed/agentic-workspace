"""Opt-in config contract for the ``auth`` service.

Exposes the rotation cadence, credential validity, and the Discord push target
on the settings page. Values are stored locally in the auth DB (via
:class:`awm.persistence.service_config.ConfigContract`) and read fresh at each
mint, so a cadence change takes effect on the next rotation with no restart.

Defaults encode the agreed contract: mint a fresh pair every **12 h**, each
valid **24 h** (two generations overlap), and push the day's login password to
the Discord ``#notifications`` channel — the same ``discord-bot`` account and
channel the ``ssh`` service already alerts through.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from awm.persistence.service_config import ConfigContract

# The ssh service posts lock alerts here; reuse the exact target so the day's
# password lands in the same #notifications channel operators already watch.
_DEFAULT_DISCORD_ACCOUNT = "discord-bot"
_DEFAULT_DISCORD_CHANNEL = "1522674357762261112"


class AuthSettings(BaseModel):
    """User-settable auth knobs."""

    mint_cadence_hours: float = Field(
        default=12.0,
        description="Hours between minting a fresh credential pair.",
    )
    validity_hours: float = Field(
        default=24.0,
        description="Hours a minted credential pair stays valid. Keep this "
                    "greater than the cadence so generations overlap and a "
                    "client never has to re-authenticate across a rotation.",
    )
    session_ttl_hours: float = Field(
        default=24.0,
        description="Sliding session lifetime: a browser cookie is refreshed to "
                    "this many hours ahead on each authenticated request.",
    )
    max_session_days: float = Field(
        default=30.0,
        description="Hard ceiling on total session age; sliding refresh stops "
                    "past this and re-login is required.",
    )
    push_enabled: bool = Field(
        default=True,
        description="Push the day's login password to Discord on each mint.",
    )
    discord_account: str = Field(
        default=_DEFAULT_DISCORD_ACCOUNT,
        description="social account id used for the password push.",
    )
    discord_channel: str = Field(
        default=_DEFAULT_DISCORD_CHANNEL,
        description="Discord channel id the password push is sent to.",
    )
    push_retry_attempts: int = Field(
        default=4,
        description="Attempts to push the login password to Discord before "
                    "giving up until the next mint. A transient failure (e.g. "
                    "a VPN blip to the peer hosting social) is retried with "
                    "doubling backoff (see push_retry_backoff_seconds); the "
                    "mint itself always succeeds regardless.",
    )
    push_retry_backoff_seconds: float = Field(
        default=30.0,
        description="Seconds to wait before the second push attempt; doubles "
                    "on each subsequent attempt.",
    )


CONTRACT = ConfigContract("auth", AuthSettings, title="Authentication")
