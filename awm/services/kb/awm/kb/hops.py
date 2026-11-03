"""Hop steps, the author filter and the graph section, over kb's hop arrays (``kb.hops`` builds them).

Every array is memory-mapped, so a hop pages in only the rows it walks. A hop ranks by personalized PageRank
from its seeds and breaks ties by the prior. It never re-ranks by meaning (precedence 5db3b97b404c877a).
"""

from __future__ import annotations

import difflib
import json
import math
import os
import re
import threading
import unicodedata
from pathlib import Path
from typing import Any

import numpy as np

ALONG = ("authors", "cites", "cited_by")
#: Seeds are the top hits, each weighted exp((score / best - 1) / SHARP), so a clear first hit takes most of
#: the mass and a wrong one does not empty the answer. 0.05 gave recall@20 0.618 on 300 cases, against 0.569
#: for the first hit's authors alone.
SEEDS = 5
SHARP = 0.05
RESTART = 0.5
TRIPS = 3
#: A surname matches a name when difflib's ratio reaches this; "Huttenhauer" finds Huttenhower.
SURNAME_RATIO = 0.8
TRIGRAM = 3
#: Papers a surname filter may name before it stops narrowing.
AUTHOR_PAPERS = 20_000
_ARRAYS = ("paper_id", "date", "prior", "trajectory", "pa_ptr", "pa_idx", "ap_ptr", "ap_idx", "ct_ptr", "ct_idx",
           "cb_ptr", "cb_idx", "surname", "sp_ptr", "sp_idx", "trigram", "ts_ptr", "ts_idx")


def fold(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z]", "", text)


def _grams(word: str) -> list[str]:
    padded = f"^{word}$"
    return sorted({padded[i:i + TRIGRAM] for i in range(len(padded) - TRIGRAM + 1)})


def _day(stamp: str) -> int:
    m = re.match(r"(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", stamp or "")
    return int(m[1]) * 10000 + int(m[2] or 1) * 100 + int(m[3] or 1) if m else 0


class Hops:
    """The hop arrays of one directory, opened on first use and again when its build changes."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._stamp: float | None = None
        self._a: dict[str, np.ndarray] = {}

    def _arrays(self) -> dict[str, np.ndarray] | None:
        meta = self.root / "meta.json"
        try:
            stamp = meta.stat().st_mtime
        except OSError:
            return None
        with self._lock:
            if stamp != self._stamp:
                self._a = {n: np.load(self.root / f"{n}.npy", mmap_mode="r") for n in _ARRAYS}
                self._stamp = stamp
            return self._a

    def status(self) -> dict[str, Any] | None:
        try:
            return json.loads((self.root / "meta.json").read_text())
        except (OSError, ValueError):
            return None

    # -- rows -------------------------------------------------------------

    def rows(self, paper_ids: list[str]) -> list[int]:
        """The rows of the papers the arrays hold, in the given order."""
        a = self._arrays()
        if a is None or not paper_ids:
            return []
        ids = a["paper_id"]
        keys = np.array([p.encode() for p in paper_ids], dtype=ids.dtype)
        at = np.searchsorted(ids, keys)
        return [int(i) for i, k in zip(at, keys) if i < len(ids) and ids[i] == k]

    def ids(self, rows: list[int]) -> list[str]:
        a = self._arrays()
        return [a["paper_id"][r].decode() for r in rows] if a is not None else []

    # -- hops -------------------------------------------------------------

    def hop(self, seeds: list[tuple[str, float]], along: str, after: str = "", before: str = "",
            limit: int = 20) -> list[tuple[str, float]]:
        """Papers one ``along`` hop from ``seeds`` ((paper id, score), best first), by personalized PageRank
        from the top :data:`SEEDS`, ties by the prior, the seeds left out; ``after`` and ``before`` bound
        the publication date."""
        if along not in ALONG:
            raise ValueError(f"along must be one of {', '.join(ALONG)}")
        a = self._arrays()
        if a is None:
            raise RuntimeError(f"no hop arrays at {self.root}")
        top: list[tuple[int, float]] = []
        for paper, s in seeds:
            for r in self.rows([paper]):
                top.append((r, float(s)))
            if len(top) == SEEDS:
                break
        if not top:
            return []
        best = top[0][1] or 1.0
        weight = {r: math.exp((s / best - 1) / SHARP) if best > 0 else 1.0 for r, s in top}
        total = sum(weight.values())
        score = _ppr(a, {r: w / total for r, w in weight.items()}, along)
        lo, hi = _day(after), _day(before)
        date, prior = a["date"], a["prior"]
        kept = [(r, v) for r, v in score.items() if r not in weight
                and (not lo or date[r] >= lo) and (not hi or (date[r] and date[r] < hi))]
        kept.sort(key=lambda rv: (-rv[1], -float(prior[rv[0]])))
        rows = [r for r, _ in kept[:limit]]
        return list(zip(self.ids(rows), (v for _, v in kept[:limit])))

    def neighbours(self, seeds: list[str], limit: int) -> dict[str, tuple[str, float]]:
        """The graph section: papers that cite, are cited by or share an author with ``seeds``, by the prior."""
        a = self._arrays()
        if a is None:
            return {}
        rows = set(self.rows(seeds))
        found: dict[int, str] = {}
        for r in rows:
            for c in (*_adj(a, "ct", r), *_adj(a, "cb", r)):
                found.setdefault(int(c), "cited-by or cites")
        for r in rows:
            for au in _adj(a, "pa", r):
                for q in _adj(a, "ap", int(au)):
                    found.setdefault(int(q), "shares an author")
        prior = a["prior"]
        ranked = sorted((r for r in found if r not in rows), key=lambda r: -float(prior[r]))[:limit * 3]
        return {p: (found[r], float(prior[r])) for p, r in zip(self.ids(ranked), ranked)}

    def prior(self, paper_ids: list[str]) -> dict[str, float]:
        a = self._arrays()
        if a is None:
            return {}
        rows = self.rows(paper_ids)
        return {p: float(a["prior"][r]) for p, r in zip(self.ids(rows), rows)}

    # -- author filter ----------------------------------------------------

    def surnames(self, name: str) -> list[str]:
        """The surnames ``name`` matches: each of its words of three letters or more, folded, against every
        surname that shares a trigram with it, by difflib's ratio."""
        a = self._arrays()
        if a is None:
            return []
        out: list[str] = []
        tri, sn = a["trigram"], a["surname"]
        for word in {fold(w) for w in re.split(r"[\s,.;]+", name) if len(fold(w)) >= 3}:
            grams = np.array([g.encode() for g in _grams(word)], dtype=tri.dtype)
            at = np.searchsorted(tri, grams)
            cand: set[int] = set()
            for i, g in zip(at, grams):
                if i < len(tri) and tri[i] == g:
                    cand.update(int(j) for j in a["ts_idx"][a["ts_ptr"][i]:a["ts_ptr"][i + 1]])
            for j in cand:
                s = sn[j].decode()
                if difflib.SequenceMatcher(None, word, s).ratio() >= SURNAME_RATIO:
                    out.append(s)
        return sorted(set(out))

    def by_author(self, name: str) -> set[str] | None:
        """The papers whose author list holds a surname ``name`` matches; None when there are no arrays."""
        a = self._arrays()
        if a is None:
            return None
        sn = a["surname"]
        rows: set[int] = set()
        for s in self.surnames(name):
            i = int(np.searchsorted(sn, np.array([s.encode()], dtype=sn.dtype))[0])
            rows.update(int(r) for r in a["sp_idx"][a["sp_ptr"][i]:a["sp_ptr"][i + 1]])
            if len(rows) > AUTHOR_PAPERS:
                break
        return set(self.ids(sorted(rows)))


def _adj(a: dict[str, np.ndarray], name: str, r: int) -> np.ndarray:
    ptr = a[f"{name}_ptr"]
    return a[f"{name}_idx"][ptr[r]:ptr[r + 1]]


def _step(a: dict[str, np.ndarray], mass: dict[int, float], along: str) -> dict[int, float]:
    """One step of the walk on the touched rows only: paper to author to paper, or along one citation."""
    out: dict[int, float] = {}
    if along == "authors":
        on_author: dict[int, float] = {}
        for p, m in mass.items():
            au = _adj(a, "pa", p)
            for x in au:
                on_author[int(x)] = on_author.get(int(x), 0.0) + m / len(au)
        for x, m in on_author.items():
            ps = _adj(a, "ap", x)
            for q in ps:
                out[int(q)] = out.get(int(q), 0.0) + m / len(ps)
        return out
    name = "ct" if along == "cites" else "cb"
    for p, m in mass.items():
        nxt = _adj(a, name, p)
        for q in nxt:
            out[int(q)] = out.get(int(q), 0.0) + m / len(nxt)
    return out


def _ppr(a: dict[str, np.ndarray], seeds: dict[int, float], along: str) -> dict[int, float]:
    score: dict[int, float] = {}
    walk = dict(seeds)
    for t in range(1, TRIPS + 1):
        walk = _step(a, walk, along)
        w = RESTART * (1 - RESTART) ** (t - 1)
        for p, m in walk.items():
            score[p] = score.get(p, 0.0) + w * m
    return score
