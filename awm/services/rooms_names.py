"""Auto-generate room names as verb-noun pairs.

Pools target sensible, evocative natural imagery so transcripts read
like place names (`babbling-brook`, `flowing-mountain`, ...). On
collision with an existing room, append ``-2``, ``-3``, … as needed.
"""

from __future__ import annotations

import random
from typing import Callable

# ~50 verbs (present participles)
VERBS: tuple[str, ...] = (
    "babbling", "flowing", "soaring", "dancing", "whispering",
    "drifting", "climbing", "racing", "dreaming", "humming",
    "weaving", "glowing", "wandering", "leaping", "hunting",
    "mending", "sailing", "building", "rambling", "singing",
    "tumbling", "wading", "stirring", "shining", "calling",
    "echoing", "kindling", "gathering", "circling", "rising",
    "twinkling", "skipping", "rolling", "settling", "humble",
    "winding", "guiding", "tracing", "watching", "blooming",
    "ranging", "tracking", "trekking", "casting", "swaying",
    "drumming", "lingering", "tending", "perching", "rooting",
)

# ~50 evocative nouns (places / natural features)
NOUNS: tuple[str, ...] = (
    "plain", "mountain", "river", "sea", "valley",
    "forest", "harbor", "lake", "meadow", "glacier",
    "canyon", "marsh", "summit", "delta", "reef",
    "plateau", "ridge", "lagoon", "prairie", "fjord",
    "brook", "creek", "spring", "grove", "thicket",
    "ravine", "cove", "isle", "bay", "cape",
    "moor", "fen", "heath", "savanna", "tundra",
    "steppe", "oasis", "dune", "highland", "lowland",
    "atoll", "strait", "channel", "wadi", "estuary",
    "barrier", "shelf", "trench", "pass", "summit",
)


def generate_candidates(rng: random.Random | None = None) -> list[str]:
    """Yield a shuffled list of all verb-noun pairs.

    The caller picks the first one not already taken in the DB. With
    ~50×50 = 2500 combinations, contention before a collision is
    negligible for the foreseeable scale.
    """
    r = rng or random.Random()
    verbs = list(VERBS)
    nouns = list(NOUNS)
    r.shuffle(verbs)
    r.shuffle(nouns)
    pairs = [f"{v}-{n}" for v in verbs for n in nouns]
    r.shuffle(pairs)
    return pairs


def pick_unique(exists: Callable[[str], bool],
                rng: random.Random | None = None) -> str:
    """Return a verb-noun room name that does not already exist.

    On collision append ``-2``, ``-3``, … to the chosen base. ``exists``
    is a predicate the caller wires up to a DB lookup.
    """
    candidates = generate_candidates(rng)
    for base in candidates:
        if not exists(base):
            return base
        # Bounded suffix walk to keep DB hits small per attempt.
        for n in range(2, 10):
            cand = f"{base}-{n}"
            if not exists(cand):
                return cand
    raise RuntimeError(
        "could not allocate a unique room name; consider expanding the pools"
    )
