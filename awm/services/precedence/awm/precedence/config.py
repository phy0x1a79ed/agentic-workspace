"""Constants for the precedence decision-archive service.

The precedence service owns its own DB (``AWM_DIR/services/precedence/precedence.db``);
all decision content lives in-DB, so there is no runtime directory dependency. The
only place a *directory* is consulted is bulk ingest (the ``import`` verb / seeding
pipeline), which reads a staging-manifest JSON produced by the scrapers.

Search is over three **per-field** embedding namespaces — a decision's ``context``,
``question`` and ``decision`` are embedded independently so a caller can query any
subset of the entry shape and score each field's match separately. Final result
order is a *usefulness* blend (relevance + reputation + recency + an explore bonus),
not raw cosine; the blend weights live here as named constants so they are tunable
without a schema change (see :func:`usefulness`).
"""

from __future__ import annotations

import math

# ---------------------------------------------------------------------------
# Per-field embedding namespaces inside the shared per-service ``embeddings`` table.
# One row per (field, decision) — source_id = decision id.
# ---------------------------------------------------------------------------
SOURCE_TYPES: dict[str, str] = {
    "context": "precedence_context",
    "question": "precedence_question",
    "decision": "precedence_decision",
}
FIELDS = ("context", "question", "decision")

# ---------------------------------------------------------------------------
# Light, open vocabularies (validated but not closed — decisions span arbitrary
# domains, unlike writing's fixed facets, so tags themselves are free text).
# ---------------------------------------------------------------------------
STATUSES = ("active", "superseded", "retired")
SOURCES = ("manual", "agent", "memory", "scope_post", "journal", "transcript")
NOTE_KINDS = ("amendment", "context-change", "supersede", "comment")

# ---------------------------------------------------------------------------
# Usefulness-ranking weights (Google-style blend, NOT raw cosine). Tunable here.
#
#   score = W_RELEVANCE * relevance          # fielded semantic / keyword match, [0,1]
#         + W_REPUTATION * reputation         # damped (upvotes - downvotes), [-1,1]
#         + W_RECENCY    * recency            # currency of the preference, (0,1]
#         + explore      * explore_bonus      # under-exposure lift, gated by the knob
#
# relevance is primary; reputation and recency adjust a tie; the explore term
# rotates low-`seen_count` entries up early so the archive gathers feedback
# instead of ossifying on the first-seeded few.
# ---------------------------------------------------------------------------
W_RELEVANCE = 1.0
W_REPUTATION = 0.35
W_RECENCY = 0.15
W_EXPLORE = 0.6  # multiplied by the per-call ``explore`` knob on top of the bonus

REPUTATION_DAMP = 3.0        # net votes needed to reach ~half the reputation ceiling
RECENCY_HALFLIFE_DAYS = 540  # a preference this old contributes half its recency term
EXPLORE_JITTER = 0.15        # random spread on the explore bonus (× explore knob)

DEFAULT_EXPLORE = 0.3        # middle value; agents pin explore=0 for the single best
KEYWORD_RELEVANCE = 0.6      # relevance floor a keyword (FTS) hit contributes

SEMANTIC_LIMIT = 300         # neighbours pulled per queried field before blending
DEFAULT_K = 20


def reputation(upvotes: int, downvotes: int) -> float:
    """Damped net vote signal in (-1, 1). 0 at parity; saturates as |net| grows."""
    net = (upvotes or 0) - (downvotes or 0)
    return net / (abs(net) + REPUTATION_DAMP)


def recency(age_days: float | None) -> float:
    """Currency of a preference in (0, 1]: 1 when just formed, halving every halflife.

    ``age_days`` is the age of the decision's ``created`` timestamp; ``None`` (no or
    unparseable timestamp) contributes 0 so undated entries don't get a recency lift.
    """
    if age_days is None:
        return 0.0
    if age_days <= 0:
        return 1.0
    return math.pow(0.5, age_days / RECENCY_HALFLIFE_DAYS)


def usefulness(
    *,
    relevance: float,
    upvotes: int,
    downvotes: int,
    seen_count: int,
    age_days: float | None,
    explore: float,
    jitter: float = 0.0,
) -> dict[str, float]:
    """Pure usefulness blend. Returns the total ``score`` plus its breakdown.

    Kept in one small pure function so the weights stay legible and the math is
    unit-testable in isolation from the DB. ``jitter`` is supplied by the caller
    (a small random value) so this function itself stays deterministic.
    """
    rep = reputation(upvotes, downvotes)
    rec = recency(age_days)
    explore_bonus = explore * (W_EXPLORE * (1.0 / (seen_count + 1)) + jitter)
    score = (
        W_RELEVANCE * relevance
        + W_REPUTATION * rep
        + W_RECENCY * rec
        + explore_bonus
    )
    return {
        "score": round(score, 6),
        "relevance": round(relevance, 6),
        "reputation": round(rep, 6),
        "recency": round(rec, 6),
        "explore": round(explore_bonus, 6),
    }
