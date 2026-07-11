"""Fleet-page config contract (columns, spawn defaults, token rate table).

An opt-in :class:`~awm.persistence.service_config.ConfigContract` stored in the
notifications service's own DB and published in its ``API_MANIFEST`` ``config``
block, so it drives the fleet page's config overlay *and* surfaces as a card in
``/ui/settings`` for free.

Three groups of settings:

- **Columns** — which roster columns show, and in what order. ``column_order``
  is the full ordered list of known column keys; ``hidden_columns`` is the
  subset the user has switched off. The page renders ``column_order`` minus
  ``hidden_columns``.
- **Spawn defaults** — the last-used (or default) model / effort / harness /
  scope the new-agent overlay prefills. The overlay writes these back on submit
  so "last used" sticks.
- **Token rate table** — per-model USD/MTok rates for the five token categories,
  plus the Opus-output $/MTok divisor. The roster's "tokens" column is rendered
  as **EOOT** (Equivalent Opus-4.8 Output Tokens): ``EOOT = Σ(tokens_cat ×
  rate_cat) ÷ divisor``. Defaults are the OpenRouter rates used by the
  self-improvement quota audit (journal ``435234e1``); every number is
  overridable in settings.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from awm.persistence.service_config import ConfigContract

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


class ModelRate(BaseModel):
    """USD per million tokens, by category (OpenRouter basis)."""

    input: float = 0.0
    output: float = 0.0
    cache_write_5m: float = 0.0
    cache_write_1h: float = 0.0
    cache_read: float = 0.0


# Substring-keyed: a transcript model id ("claude-opus-4-8[1m]") is matched to a
# rate by the first key that appears in its lowercased form.
DEFAULT_RATES: dict[str, ModelRate] = {
    "opus":   ModelRate(input=5,  output=25, cache_write_5m=6.25,  cache_write_1h=10, cache_read=0.50),
    "sonnet": ModelRate(input=2,  output=10, cache_write_5m=2.50,  cache_write_1h=4,  cache_read=0.20),
    "fable":  ModelRate(input=10, output=50, cache_write_5m=12.50, cache_write_1h=20, cache_read=1.00),
    "haiku":  ModelRate(input=1,  output=5,  cache_write_5m=1.25,  cache_write_1h=2,  cache_read=0.10),
}


class SpawnDefaults(BaseModel):
    """Prefill for the new-agent overlay (written back as 'last used')."""

    harness: str = "claude"
    model: str = "sonnet"
    effort: str = "medium"
    scope: str = ""            # last-used worktree path (blank = none yet)


class FleetSettings(BaseModel):
    """Everything the fleet page persists via the notifications DB."""

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


FLEET_CONTRACT = ConfigContract(
    "notifications", FleetSettings, title="Fleet view",
)


def rate_for_model(rates: dict[str, ModelRate], model: Optional[str]) -> Optional[ModelRate]:
    """Match a transcript model id to a rate by substring; None if unknown."""
    if not model:
        return None
    m = model.lower()
    for key, rate in rates.items():
        if key.lower() in m:
            return rate
    return None


def eoot(
    tok_in: int, tok_out: int, tok_cw5: int, tok_cw1: int, tok_cr: int,
    *, model: Optional[str], rates: dict[str, ModelRate], divisor: float,
) -> Optional[float]:
    """Equivalent Opus-output tokens for a session's cumulative usage.

    ``EOOT = Σ(tokens_cat × rate_cat) ÷ divisor`` — token units cancel to
    "Opus-4.8 output tokens" (both rates and divisor are $/MTok). Returns None
    when the model is unknown (can't price it) so the UI can show a dash rather
    than a misleading zero.
    """
    rate = rate_for_model(rates, model)
    if rate is None or divisor <= 0:
        return None
    cost_num = (
        tok_in * rate.input
        + tok_out * rate.output
        + tok_cw5 * rate.cache_write_5m
        + tok_cw1 * rate.cache_write_1h
        + tok_cr * rate.cache_read
    )
    return cost_num / divisor
