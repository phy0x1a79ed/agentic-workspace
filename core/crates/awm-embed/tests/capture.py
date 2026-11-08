"""Capture persistence.embeddings' chunker, header and hashes on synthetic documents.

Run from the crate root: ~/miniforge3/bin/mamba run -n awm python tests/capture.py <awm checkout>
Writes tests/fixtures/chunker.json. Every input is synthetic.
"""

from __future__ import annotations

import hashlib
import json
import random
import sys
from pathlib import Path

assert sys.version_info[:2] == (3, 12)
sys.path.insert(0, str(Path(sys.argv[1]) / "service_components" / "persistence"))
from awm.persistence import embeddings as engine  # noqa: E402

RNG = random.Random(20261107)
WORDS = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau "
         "upsilon phi chi psi omega deploy release scope journal search index chunk header vector").split()
ODD = ["café", "naïve", "日本語", "👍🏽", "x²", "٣", "snake_case", "e.g.", "Dr.", "end?!", "ﬁle", "İ", "ΟΔΟΣ", " ", "\x1c", " "]


def sentence() -> str:
    n = RNG.randint(1, 30)
    words = [RNG.choice(WORDS + ODD if RNG.random() < 0.15 else WORDS) for _ in range(n)]
    return " ".join(words) + RNG.choice([".", "!", "?", "", ".", "..."])


def paragraph() -> str:
    return " ".join(sentence() for _ in range(RNG.randint(1, 12)))


def body() -> str:
    parts = []
    for _ in range(RNG.randint(0, 14)):
        kind = RNG.random()
        if kind < 0.15:
            parts.append(RNG.choice(["#", "##", "###", "######", "#######"]) + " " + sentence())
        elif kind < 0.22:
            parts.append(f"**{sentence()[:RNG.randint(1, 90)]}**" + RNG.choice(["", ":", ": "]))
        elif kind < 0.40:
            parts.append("\n".join(RNG.choice(["- ", "* ", "+ ", "1. ", "12) ", "-", "3.x "]) + sentence()
                                   for _ in range(RNG.randint(1, 6))))
        elif kind < 0.45:
            parts.append(" ".join(RNG.choice(WORDS) for _ in range(RNG.randint(150, 260))))
        elif kind < 0.48:
            parts.append("x" * RNG.randint(100, 1200))
        else:
            parts.append(paragraph())
    sep = RNG.choice(["\n\n", "\n", "\n\n\n", "\r\n\r\n", "\n \n"])
    return sep.join(parts)


def doc(i: int) -> engine.Document:
    return engine.Document(
        source_id=f"id{i:04x}", body=body(),
        title=RNG.choice(["", " ", sentence(), "  padded title  ", "t" * 300, "日本語 title"]),
        context=RNG.choice(["", "proj/scope", "awm/port-knowledge · x"]),
        date=RNG.choice(["", "2026-11-06", "2024-02-29"]))


def main() -> None:
    cases = []
    for i in range(160):
        d = doc(i)
        approx = engine.chunk(d.body, engine.CHUNK_TOKENS)
        small = engine.chunk(d.body, 16)
        cases.append({
            "title": d.title, "context": d.context, "date": d.date, "body": d.body,
            "approx_tokens": engine._approx_tokens([d.body])[0],
            "chunks": approx, "chunks16": small,
            "header": engine.header(d, approx[0][0] if approx else ""),
            "content_hash": engine.content_hash(d),
            "chunks_of": engine._chunks_of(d, engine._approx_tokens),
            "search_hash": hashlib.sha1(f"1\x00{d.title}".encode()).hexdigest(),
        })
    out = Path(__file__).parent / "fixtures" / "chunker.json"
    out.write_text(json.dumps(cases, ensure_ascii=True, indent=1) + "\n")
    print(len(cases), sum(len(c["chunks"]) for c in cases), "chunks")


if __name__ == "__main__":
    main()
