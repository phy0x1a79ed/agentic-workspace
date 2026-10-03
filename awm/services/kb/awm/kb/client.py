"""HTTP calls to the kb server. One function per route the service uses."""

from __future__ import annotations

from typing import Any

import httpx

from awm.kb import instances


class KbError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"kb {status}: {detail}")
        self.status = status
        self.detail = detail


async def _req(method: str, path: str, *, json: Any = None, params: dict | None = None,
               timeout: float = 30.0) -> Any:
    async with httpx.AsyncClient(base_url=instances.URL, timeout=timeout) as c:
        r = await c.request(method, path, json=json, params=params)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        raise KbError(r.status_code, str(detail))
    return r.json()


async def status() -> dict:
    return await _req("GET", "/status", timeout=10)


async def recall(body: dict, *, timeout: float = 600) -> dict:
    return await _req("POST", "/recall", json=body, timeout=timeout)


async def upsert(posts: list[dict]) -> dict:
    return await _req("POST", "/posts/upsert", json={"posts": posts}, timeout=120)


async def sweep(ids: list[str], *, full: bool = True) -> dict:
    return await _req("POST", "/posts/sweep", json={"ids": ids, "full": full}, timeout=120)


async def zotero_sync() -> dict:
    return await _req("POST", "/zotero/sync")


async def quiesce(timeout: float = 900) -> dict:
    return await _req("POST", "/quiesce", params={"timeout": timeout}, timeout=timeout + 30)


async def resume() -> dict:
    return await _req("POST", "/resume")
