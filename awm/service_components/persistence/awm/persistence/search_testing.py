"""A deterministic embedder for tests of anything that indexes or searches."""

from __future__ import annotations

import hashlib
import re

import numpy as np


class StubEmbedder:
    """Bag-of-words hashed into 64 dims: texts that share words score high."""

    def __init__(self, name: str = "stub-a"):
        self.name = name
        self.encoded = 0

    def count_tokens(self, texts):
        return [len(re.findall(r"\w+|[^\w\s]", t)) for t in texts]

    def encode(self, texts, *, query):
        self.encoded += len(texts)
        out = np.zeros((len(texts), 64), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in re.findall(r"\w+", t.lower()):
                out[i, int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1
        out /= np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)
        return out
