"""Opt-in config contract for the ``auth`` service.

Exposes the rotation cadence, credential validity, and the Discord push target
on the settings page. Values are stored locally in the auth DB (via
:class:`awm.persistence.service_config.ConfigContract`) and read fresh at each
mint, so a cadence change takes effect on the next rotation with no restart.

Defaults encode the agreed contract: mint a fresh pair every **12 h**, each
valid **24 h** (two generations overlap), and push the day's login password to
the node's operator DM — the same target the ``ssh`` service alerts through.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from awm.persistence.service_config import ConfigContract


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
    lockout_threshold: int = Field(
        default=6,
        description="Consecutive failed logins from one client IP before "
                    "further attempts from it are refused for a while.",
    )
    lockout_minutes: float = Field(
        default=15.0,
        description="How long a client IP stays locked after "
                    "reaching lockout_threshold.",
    )
    push_enabled: bool = Field(
        default=True,
        description="Push the day's login password to the Ops note on each mint.",
    )
    push_discord: bool = Field(
        default=False,
        description="Send the password push to Discord through social, in "
                    "place of the reporting service's Ops note.",
    )
    discord_account: str = Field(
        default="",
        description="social account id used for the password push. Empty "
                    "uses the node's operator DM account.",
    )
    discord_channel: str = Field(
        default="",
        description="Discord channel id the password push is sent to. Empty "
                    "uses the node's operator DM (AWM_OPERATOR_DM_CHANNEL).",
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
