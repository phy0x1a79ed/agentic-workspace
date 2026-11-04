"""Opt-in config contract for the ``auth`` service.

Exposes the session lifetime on the settings page. Values are stored locally in
the auth DB (via :class:`awm.persistence.service_config.ConfigContract`) and
read fresh on each edge material fetch.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from awm.persistence.service_config import ConfigContract


class AuthSettings(BaseModel):
    """User-settable auth knobs."""

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
