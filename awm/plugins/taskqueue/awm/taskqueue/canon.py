"""Canonical JSON: the byte form every signature and MAC in the queue and the approver covers."""

from __future__ import annotations

import json
from typing import Any


def canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode()
