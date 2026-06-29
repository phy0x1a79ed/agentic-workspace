"""The agents service's default-driver config contract.

The first real :class:`~awm.persistence.service_config.ConfigContract` wired
into awm: which harness (and optionally model) a newly *dispatched* placement
uses **by default**. The default resolution lives in
:func:`awm.agents.placement.place_on_task` (the orchestrated path), where the
stored value sits between an explicit ``args``/env override and the historical
``"opencode"`` literal — so changing it in the settings UI changes which driver
new unattended task work spawns under, while explicit overrides still win.

Values are stored **locally** in the agents service's own DB; the schema is
published in the agents ``API_MANIFEST`` ``config`` block and surfaced by the
gateway aggregator. A direct ``create_session`` (attended terminal) is a
separate path with its own ``"claude"`` default and is intentionally not
governed by this contract.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from awm.persistence.service_config import ConfigContract


class DriverSettings(BaseModel):
    """User-settable defaults for a newly dispatched placement."""

    harness: Literal["opencode", "claude"] = Field(
        default="opencode",
        description="Which agent CLI a new placement spawns under by default.",
    )
    model: Optional[str] = Field(
        default=None,
        description=(
            "Model override for new placements (blank → the harness's own "
            "default: DSv4-free for opencode, the CLI default for claude)."
        ),
    )


# Module-level singleton: the adapter splices its manifest fragment + handlers,
# and placement.py reads its live value per dispatch.
DRIVER_CONTRACT = ConfigContract(
    "agents", DriverSettings, title="Agent default driver"
)
