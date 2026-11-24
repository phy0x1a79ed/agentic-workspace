"""The page's conversation with a study's curator, as a guest in the curator's channel room.

The curator session opens a persistent room and seats it here with its link. The relay keeps the
room's messages, so this module stores only each study's link.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

from awm.config import canonical_workspace


def seats_file() -> Path:
    state = Path(os.environ.get("AWM_DIR") or canonical_workspace() / ".awm") / "services" / "scopeview"
    state.mkdir(parents=True, exist_ok=True)
    return state / "seats.json"


def seats() -> dict:
    f = seats_file()
    return json.loads(f.read_text()) if f.exists() else {}


def seat(project: str, scope: str, link: str, by: str) -> dict:
    if not link.startswith("https://") or "/room/" not in link:
        raise ValueError("link is a channel room link, https://<relay>/room/<name>")
    all_ = seats()
    all_[f"{project}/{scope}"] = {"link": link.rstrip("/"), "by": by}
    seats_file().write_text(json.dumps(all_, indent=2))
    return all_[f"{project}/{scope}"]


def room(project: str, scope: str) -> str:
    s = seats().get(f"{project}/{scope}")
    if not s:
        raise LookupError(f"no curator is seated in {project}/{scope}")
    return s["link"]


async def read(project: str, scope: str, after: int, wait: float) -> dict:
    link = room(project, scope)
    async with httpx.AsyncClient(timeout=40) as cli:
        r = await cli.get(f"{link}/recv", params={"after": after, "wait": int(min(wait, 25))})
    r.raise_for_status()
    return {"room": link.rsplit("/", 1)[1], **r.json()}


async def say(project: str, scope: str, text: str, context: dict) -> dict:
    link = room(project, scope)
    async with httpx.AsyncClient(timeout=30) as cli:
        about = " ".join(f"{k}={v}" for k, v in context.items() if v)
        r = await cli.post(f"{link}/send", json={"from": "scopeview", "text": f"[{about}]\n{text}" if about else text})
    r.raise_for_status()
    return r.json()
