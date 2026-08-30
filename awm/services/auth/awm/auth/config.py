"""Opt-in config contract for the ``auth`` service.

Exposes the rotation cadence, credential validity, and the Discord push target
on the settings page. Values are stored locally in the auth DB (via
:class:`awm.persistence.service_config.ConfigContract`) and read fresh at each
mint, so a cadence change takes effect on the next rotation with no restart.

Defaults encode the agreed contract: mint a fresh pair every **12 h**, each
valid **24 h** (two generations overlap), and push the day's login password to
the Discord ``#notifications`` channel — the same ``discord-bot`` account and
channel the ``ssh`` service already alerts through.

The Penpot rotation hour is here rather than beside the cadences above because
it is a different kind of knob: the shared credential rotates on an *interval*,
which no one has to be awake for, while a Penpot rotation ends everyone's live
Penpot session and so has to land at an *hour* nobody is drawing in.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, Field

from awm.persistence.service_config import ConfigContract

# The ssh service posts lock alerts here; reuse the exact target so the day's
# password lands in the same #notifications channel operators already watch.
_DEFAULT_DISCORD_ACCOUNT = "discord-bot"
_DEFAULT_DISCORD_CHANNEL = "1522674357762261112"

#: Lets a host state its own Penpot rotation hour in ``/etc/awm/env`` rather
#: than leaving it to whoever last opened the settings page. Read once, at
#: import, so it is the *default* a fresh box starts from and a stored override
#: still wins — which is the right precedence: the env file is provisioning,
#: the settings page is a decision someone made afterwards.
_ROTATION_HOUR_ENV = "AWM_PENPOT_ROTATION_HOUR"


def _default_rotation_hour() -> int:
    try:
        return int((os.environ.get(_ROTATION_HOUR_ENV) or "").strip()) % 24
    except ValueError:
        return 4


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
        description="Consecutive failed logins (per username and per client "
                    "IP) before further attempts are refused for a while.",
    )
    lockout_minutes: float = Field(
        default=15.0,
        description="How long a username or client IP stays locked after "
                    "reaching lockout_threshold.",
    )
    penpot_rotation_hour: int = Field(
        default=_default_rotation_hour(),
        description="Local hour (0-23) at which every stored Penpot password "
                    "is replaced. Rotating logs people out of Penpot, so this "
                    "wants to be an hour nobody is drawing in.",
    )
    penpot_rotation_enabled: bool = Field(
        default=True,
        description="Replace stored Penpot passwords nightly. Turn off only to "
                    "hold a credential still while diagnosing one that has "
                    "drifted out of step with Penpot's own profile.",
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
