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


async def related(body: dict, *, timeout: float = 10) -> dict:
    """Entry-index hits per zone, and with `nouns` the nearest entities."""
    return await _req("POST", "/related", json=body, timeout=timeout)


async def graph(body: dict, *, timeout: float = 70) -> dict:
    return await _req("POST", "/graph", json=body, timeout=timeout)


async def schema() -> dict:
    return await _req("GET", "/schema", timeout=10)


async def zotero_sync() -> dict:
    return await _req("POST", "/zotero/sync")


async def quiesce(timeout: float = 900) -> dict:
    return await _req("POST", "/quiesce", params={"timeout": timeout}, timeout=timeout + 30)


async def resume() -> dict:
    return await _req("POST", "/resume")
