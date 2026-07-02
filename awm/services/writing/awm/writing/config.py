"""Controlled tag vocabulary + corpus-source defaults for the writing service.

The writing service owns its own DB (``AWM_DIR/services/writing/writing.db``);
sample content lives in-DB, so there is no runtime corpus-directory dependency.
The only place a corpus *directory* is still consulted is bulk ingest / one-time
migration (``import`` verb, ``seed.py``), which reads a manifest.json + text
files — that path defaults to the historical self-improvement corpus.
"""

from __future__ import annotations

from pathlib import Path

# Embedding namespace inside the per-service ``embeddings`` table.
SOURCE_TYPE = "writing_sample"


def default_corpus_dir() -> Path:
    """Historical corpus source (manifest.json + text files) for bulk ingest.

    Resolved lazily against the live workspace root so it follows AWM_WORKSPACE.
    """
    from awm.config import DATA_DIR

    return DATA_DIR / "self-improvement" / "writing_samples"


# ---------------------------------------------------------------------------
# Controlled tag vocabulary. Tags are stored as (facet, value) pairs; keeping
# the vocabulary closed is what makes ``tag=facet:value`` filtering reliable.
# ``form`` and ``register`` may have multiple values per sample; ``grade`` is
# single (and mirrored onto samples.grade for convenience).
# ---------------------------------------------------------------------------
VOCAB: dict[str, list[str]] = {
    "form": [
        "essay",            # argumentative/analytical academic essay
        "lab-report",       # lab writeup (abstract/methods/results/discussion prose)
        "research-summary", # research summary / progress report / annotated bib
        "manuscript",       # research paper / manuscript / genome announcement
        "proposal",         # thesis / grant / research proposal
        "statement",        # research/personal/award statement
        "cover-letter",     # job application cover letter
        "reflection",       # personal reflective prose
        "speaking-notes",   # presentation script / speaking notes
        "encyclopedic",     # wikipedia-style / reference prose
        "creative",         # creative / fiction / personal narrative
        "homework",         # short-answer homework written as prose
    ],
    "register": [
        "argumentative",  # stakes a thesis and defends it
        "persuasive",     # advocacy/application prose (proposals, cover letters)
        "expository",     # explains/analyses without arguing a side
        "technical",      # methods/results, scientific register
        "narrative",      # storytelling
        "personal",       # first-person reflective
    ],
    "grade": [
        "style-grade",  # clean, representative prose — the style-reference feed
        "acceptable",   # usable but not exemplary
        "weak",         # fragment / outline-ish / scratch
    ],
}

SINGLE_VALUE_FACETS = {"grade"}


def validate_tag(facet: str, value: str) -> None:
    if facet not in VOCAB:
        raise ValueError(f"unknown facet {facet!r}; allowed: {', '.join(VOCAB)}")
    if value not in VOCAB[facet]:
        raise ValueError(
            f"unknown value {value!r} for facet {facet!r}; allowed: {', '.join(VOCAB[facet])}"
        )
