"""The agents service's default-driver config contract.

The first real :class:`~awm.persistence.service_config.ConfigContract` wired
into awm: which harness, model, and reasoning effort a newly *dispatched*
placement uses **by default**. The default resolution lives in
:func:`awm.agents.placement.place_on_task` (the orchestrated path), where the
stored value sits between an explicit ``args``/env override and the field
default — so changing it in the settings UI changes which driver new task work
spawns under, while explicit overrides still win.

**Uniform placement (T5).** Every placement — attended or detached — defaults
to the SAME driver: ``claude`` / ``haiku`` / ``medium`` effort. The old
dispatch-side fork (attached → claude, detached → free opencode) is gone: there
are no second-class detached nodes. The opencode backend stays available as an
explicit override (``args``/env/a saved contract value), but nothing forks on
attach state automatically.

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
    """User-settable defaults for a newly dispatched placement.

    Uniform default (T5): ``claude`` / ``haiku`` / ``medium`` for EVERY
    placement regardless of attach state."""

    harness: Literal["opencode", "claude"] = Field(
        default="claude",
        description="Which agent CLI a new placement spawns under by default.",
    )
    model: Optional[str] = Field(
        default="haiku",
        description=(
            "Model override for new placements. Blank does NOT inherit the "
            "operator's ambient CLI/ANTHROPIC_MODEL default: placement "
            "resolution fills a concrete per-harness default (claude → "
            "haiku, opencode → deepseek-v4-flash-free) so every spawn "
            "carries an explicit model (hard-required at the spawn boundary)."
        ),
    )
    effort: Optional[str] = Field(
        default="medium",
        description=(
            "Reasoning effort for new placements (claude only — opencode "
            "ignores it). One of low/medium/high/xhigh/max, or blank for the "
            "CLI default."
        ),
    )


# Module-level singleton: the adapter splices its manifest fragment + handlers,
# and placement.py reads its live value per dispatch.
DRIVER_CONTRACT = ConfigContract(
    "agents", DriverSettings, title="Agent default driver"
)
