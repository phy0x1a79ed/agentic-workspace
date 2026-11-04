"""Opt-in config contract for the ``auth`` service.

Exposes the peer-credential rotation cadence and validity, and the session
lifetime, on the settings page. Values are stored locally in the auth DB (via
:class:`awm.persistence.service_config.ConfigContract`) and read fresh at each
mint, so a cadence change takes effect on the next rotation with no restart.

Defaults encode the agreed contract: mint a fresh peer credential every
**12 h**, each valid **24 h**, so two generations overlap.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from awm.persistence.service_config import ConfigContract


class AuthSettings(BaseModel):
    """User-settable auth knobs."""

    mint_cadence_hours: float = Field(
        default=12.0,
        description="Hours between minting a fresh peer credential.",
    )
    validity_hours: float = Field(
        default=24.0,
        description="Hours a minted peer credential stays valid. Keep this "
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


CONTRACT = ConfigContract("auth", AuthSettings, title="Authentication")
