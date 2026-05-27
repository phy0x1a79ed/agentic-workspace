"""Build room-name word lists from WordNet + wordfreq, with curation overlay.

Idempotent: same inputs produce byte-identical output files. Build-time only —
not imported by the running service. Run via:

    python -m awm.scripts.build_room_names

Inputs:
  data/awm/room_names/{adjectives,nouns}.md  — human curation overlay
  data/awm/room_names/blocklist.txt          — hard exclude list

Outputs (ship inside the package, read at runtime by `rooms_names.py`):
  awm/services/_room_names_data/adjectives.txt
  awm/services/_room_names_data/nouns.txt
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from awm.config import DATA_DIR

ZIPF_MIN = 3.0
SYLL_MAX = 3

OVERLAY = DATA_DIR / "awm" / "room_names"
OUT = Path(__file__).resolve().parent.parent / "services" / "_room_names_data"
CACHE = OVERLAY / ".cache"
NLTK_CACHE = CACHE / "nltk"

import nltk
NLTK_CACHE.mkdir(parents=True, exist_ok=True)
nltk.data.path.insert(0, str(NLTK_CACHE))
for pkg, probe in (("wordnet", "corpora/wordnet"), ("cmudict", "corpora/cmudict")):
    try:
        nltk.data.find(probe)
    except LookupError:
        nltk.download(pkg, download_dir=str(NLTK_CACHE), quiet=True)

from nltk.corpus import wordnet as wn, cmudict  # noqa: E402
from wordfreq import zipf_frequency  # noqa: E402

CMU = cmudict.dict()

FORCE_ADJ = ("demonic", "deadly", "cursed", "wicked", "holy", "royal", "noble")
FORCE_NOUN = ("camp", "squad", "den", "lair", "keep", "realm",
              "sofa", "heaven", "hell")

PLACE_SYNSETS = (
    "location.n.01",
    "structure.n.01",
    "celestial_body.n.01",
    "facility.n.01",
    "tract.n.01",
    "body_of_water.n.01",
    "region.n.03",
)


def syllables(word: str) -> int:
    if word in CMU:
        phones = CMU[word][0]
        return sum(1 for p in phones if p[-1].isdigit())
    vowels = re.findall(r"[aeiouy]+", word)
    n = len(vowels)
    if word.endswith("e") and n > 1:
        n -= 1
    return max(n, 1)


def alpha_ok(w: str) -> bool:
    return bool(re.fullmatch(r"[a-z]+", w))


def common(w: str) -> bool:
    return zipf_frequency(w, "en") >= ZIPF_MIN


def short(w: str) -> bool:
    return syllables(w) <= SYLL_MAX


def adj_candidates() -> set[str]:
    out: set[str] = set()
    for pos in ("a", "s"):
        for syn in wn.all_synsets(pos):
            for lemma in syn.lemmas():
                out.add(lemma.name().lower())
    return out


def noun_candidates() -> set[str]:
    out: set[str] = set()
    for name in PLACE_SYNSETS:
        ss = wn.synset(name)
        for h in ss.closure(lambda s: s.hyponyms()):
            for lemma in h.lemmas():
                out.add(lemma.name().lower())
    return out


_RE_TICK = re.compile(r"^\s*-\s*\[x\]\s+([a-z]+)", re.IGNORECASE)


def parse_overlay(md: Path) -> tuple[set[str], set[str]]:
    """Return (drops, adds) from the curation `.md`.

    Default: `[x]` means drop. Inside a `## Candidates to add` section,
    `[x]` means add — unless a paragraph mentioning "drop" or "swap-out"
    intervenes, which flips back to drop until the next H2.
    """
    if not md.exists():
        return set(), set()
    drops: set[str] = set()
    adds: set[str] = set()
    is_add_section = False
    sub_mode_is_drop = False
    for line in md.read_text().splitlines():
        if line.startswith("## "):
            is_add_section = "candidates to add" in line.lower()
            sub_mode_is_drop = False
            continue
        if is_add_section and line.strip() and not line.lstrip().startswith("-"):
            low = line.lower()
            if "drop" in low or "swap-out" in low or "swap out" in low:
                sub_mode_is_drop = True
        m = _RE_TICK.match(line)
        if m:
            word = m.group(1).lower()
            if is_add_section and not sub_mode_is_drop:
                adds.add(word)
            else:
                drops.add(word)
    return drops, adds


def load_blocklist() -> set[str]:
    f = OVERLAY / "blocklist.txt"
    if not f.exists():
        return set()
    out: set[str] = set()
    for line in f.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.add(s.lower())
    return out


def build(candidates: set[str], drops: set[str], adds: set[str],
          block: set[str], forced: tuple[str, ...],
          extra_filter=lambda _w: True) -> list[str]:
    pool: set[str] = set()
    for w in candidates:
        if not alpha_ok(w):
            continue
        if not short(w):
            continue
        if not common(w):
            continue
        if not extra_filter(w):
            continue
        if w in block or w in drops:
            continue
        pool.add(w)
    for w in tuple(forced) + tuple(adds):
        if alpha_ok(w) and short(w) and w not in block:
            pool.add(w)
    return sorted(pool)


def main() -> int:
    block = load_blocklist()
    adj_drops, adj_adds = parse_overlay(OVERLAY / "adjectives.md")
    noun_drops, noun_adds = parse_overlay(OVERLAY / "nouns.md")

    adj = build(
        adj_candidates(), adj_drops, adj_adds, block, FORCE_ADJ,
        extra_filter=lambda w: not w.endswith("ing"),
    )
    noun = build(
        noun_candidates(), noun_drops, noun_adds, block, FORCE_NOUN,
    )

    for w in FORCE_ADJ:
        assert w in adj, f"force-include missing from adj: {w}"
    for w in FORCE_NOUN:
        assert w in noun, f"force-include missing from noun: {w}"
    for w in adj + noun:
        assert alpha_ok(w), f"non-alpha word in output: {w!r}"
        assert syllables(w) <= SYLL_MAX, f"too long ({syllables(w)} syll): {w}"

    product = len(adj) * len(noun)
    if product < 100_000:
        print(
            f"FAIL: |adj|={len(adj)} |noun|={len(noun)} product={product:,} "
            f"< 100,000 floor",
            file=sys.stderr,
        )
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "adjectives.txt").write_text("\n".join(adj) + "\n")
    (OUT / "nouns.txt").write_text("\n".join(noun) + "\n")
    print(
        f"wrote |adj|={len(adj)} |noun|={len(noun)} product={product:,}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
