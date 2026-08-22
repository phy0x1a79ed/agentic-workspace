"""Fleet-view config contract (columns, spawn defaults, token rate table).

Absorbed from the retired notifications service. An opt-in
:class:`~awm.persistence.service_config.ConfigContract` stored in the **agents**
service's own DB — but under a DISTINCT storage key (``"fleet"``) so it never
collides with the agents driver contract (``DriverSettings``, stored under the
default ``"service_config"`` key). The two contracts share one ``awm_settings``
table in ``agents.db`` and stay independent rows.

**Surfacing.** The config aggregator's ``config_get``/``config_set`` handler
pair is a fixed, single-per-service slot already owned by the driver contract,
so the fleet contract is NOT spliced there. It is reached instead by dedicated
agents verbs (``get_fleet_config`` / ``save_fleet_config``) that call this
contract's ``get`` / ``set``, and its live values ride along inside every
``list_fleet`` response, so the fleet page's first paint needs a single fetch.

Three groups of settings:

- **Columns** — which roster columns show, and in what order.
- **Spawn defaults** — the last-used model / effort / harness / scope the
  new-agent overlay prefills (written back on submit so "last used" sticks).
- **Token rate table** — per-model USD/MTok rates + the Opus-output $/MTok
  divisor driving the EOOT column (see :mod:`accounting`).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from awm.persistence.service_config import ConfigContract

from .accounting import DEFAULT_RATES, ModelRate

# Canonical roster column keys the fleet page knows how to render. Kept here so
# the contract's default order and the page stay in one authority.
KNOWN_COLUMNS = [
    "status",       # icon + keyword (working / idle / needs-you / error / ended)
    "title",        # session title or project label
    "harness",      # claude | opencode
    "model",        # resolved model (from transcript usage)
    "uptime",       # first_seen → now
    "last_activity",  # last_seen → now
    "attention",    # open attention items (count / kinds)
    "attachable",   # tmux handle present?
    "tokens",       # cumulative cost as EOOT
    "context",      # current context length (tokens)
    "dispose",      # two-tap teardown control
]

# The `status` column is hidden by default: the roster already groups rows into
# status sections (each with its own icon + count), so a per-row status cell is
# redundant. It stays a known column so the settings page can switch it back on.
DEFAULT_HIDDEN = ["status", "model", "last_activity"]


class SpawnDefaults(BaseModel):
    """Prefill for the new-agent overlay (written back as 'last used')."""

    harness: str = "claude"
    model: str = "sonnet"
    effort: str = "medium"
    scope: str = ""            # last-used worktree path (blank = none yet)


class FleetSettings(BaseModel):
    """Everything the fleet page persists via the agents DB."""

    column_order: list[str] = Field(default_factory=lambda: list(KNOWN_COLUMNS))
    hidden_columns: list[str] = Field(default_factory=lambda: list(DEFAULT_HIDDEN))
    spawn_defaults: SpawnDefaults = Field(default_factory=SpawnDefaults)
    notifications_enabled: bool = Field(
        default=True,
        description="App-level gate for desktop pushes. The browser permission is "
                    "still required; this lets the user mute pushes without "
                    "revoking permission (which the browser won't allow anyway).",
    )
    rates: dict[str, ModelRate] = Field(default_factory=lambda: dict(DEFAULT_RATES))
    eoot_divisor_usd_mtok: float = Field(
        default=25.0,
        description="Opus-4.8 output $/MTok — the EOOT normalization divisor.",
    )
    liveness_window_s: float = Field(
        default=12 * 3600.0,
        description="Live sessions last seen within this window appear in the "
                    "roster.",
    )
    ended_window_s: float = Field(
        default=600.0,
        description="A finished ('ended') session drops off the roster this long "
                    "after its last signal — much shorter than the live window so "
                    "a just-finished agent shows briefly without a long-dead one "
                    "lingering as a duplicate of a fresh session in the same cwd.",
    )


# Module-level singleton: stored in the agents DB under the distinct "fleet"
# key (the driver contract owns "service_config"), surfaced via the dedicated
# get_fleet_config / save_fleet_config verbs and inline in list_fleet.
FLEET_CONTRACT = ConfigContract(
    "agents", FleetSettings, title="Fleet view", key="fleet",
)
