"""Front matter and cell values, shared by the graph builder and the tracer."""
from __future__ import annotations

import re
from datetime import date

import yaml

FRONT = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)


def split(text: str) -> tuple[dict, str]:
    m = FRONT.match(text)
    if not m:
        raise ValueError("no front matter")
    return yaml.safe_load(m.group(1)) or {}, m.group(2)


def join(meta: dict, body: str) -> str:
    return f"---\n{yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=120)}---\n{body}"


def clip(v):
    if isinstance(v, (bytes, bytearray)):
        return f"<{len(v)} bytes>"
    if isinstance(v, (list, dict)):
        v = str(v)
    if isinstance(v, str) and len(v) > 300:
        return v[:300] + "…"
    if isinstance(v, date):
        return v.isoformat()
    return v
