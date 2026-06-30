"""Mira connector — a thin client of the mira API daemon.

For an account with ``source = "mira"``, all platform logic lives on the mira
host (see ``awm/services/social/mira/``): a network daemon drives the logged-in
Opera sessions (Slack web + Teams web) and exposes a clean REST + WebSocket API.
This connector is a pure shim:

  * ``send`` / ``list_channels`` / ``identity`` → REST calls to the daemon.
  * ``start`` → opens the daemon's ``/v1/events`` WebSocket and forwards each
    inbound message (for this account's platform) to ``on_message``. The daemon
    does the polling on mira, so there is **no client-side poll loop** here.

One class serves both Slack-via-mira and Teams — the platform comes from
``account.platform`` and selects the daemon's ``/v1/{platform}/…`` routes. The
daemon uses a self-signed cert, so TLS verification defaults off
(``mira_verify_tls``); pin the cert later to turn it on.
"""

from __future__ import annotations

import asyncio
import logging

from awm.social.connectors.base import (
    Account, Attachment, Channel, Connector, Identity, InboundMessage,
    OnMessage,
)

log = logging.getLogger("awm.social.connectors.mira")

WS_RETRY_MAX_S = 30.0


def _to_attachments(items: list) -> list[Attachment]:
    """Map the daemon's normalised attachment dicts to :class:`Attachment`."""
    out: list[Attachment] = []
    for a in items or []:
        out.append(Attachment(
            idx=int(a.get("idx", 0) or 0),
            filename=a.get("filename", "") or "",
            mime=a.get("mime", "") or "",
            size=int(a.get("size", 0) or 0),
            url=a.get("url", "") or "",
            ref=a.get("ref") or {},
        ))
    return out


class MiraConnector(Connector):
    platform = "mira"  # overridden per-instance by the account's real platform

    def __init__(self, account: Account, on_message: OnMessage) -> None:
        super().__init__(account, on_message)
        self.platform = account.platform
        self._base = (account.mira_url or "").rstrip("/")
        self._token = account.mira_token or ""
        self._verify = account.mira_verify_tls
        self._session = None  # aiohttp.ClientSession
        self._ws = None
        self._self_id = ""

    # -- transport ----------------------------------------------------------

    def _connector(self):
        import aiohttp

        # ssl=False disables verification for the daemon's self-signed cert.
        return aiohttp.TCPConnector(ssl=None if self._verify else False)

    async def _ensure_session(self):
        import aiohttp

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=self._connector(),
                headers={"Authorization": f"Bearer {self._token}"})
        return self._session

    async def _get(self, path: str, **params) -> dict:
        import aiohttp

        sess = await self._ensure_session()
        async with sess.get(self._base + path, params=params,
                            timeout=aiohttp.ClientTimeout(total=30)) as r:
            data = await r.json()
            if r.status >= 400:
                raise RuntimeError(f"mira GET {path} -> {r.status}: {data.get('error')}")
            return data

    async def _post(self, path: str, body: dict) -> dict:
        import aiohttp

        sess = await self._ensure_session()
        async with sess.post(self._base + path, json=body,
                            timeout=aiohttp.ClientTimeout(total=30)) as r:
            data = await r.json()
            if r.status >= 400:
                raise RuntimeError(f"mira POST {path} -> {r.status}: {data.get('error')}")
            return data

    # -- operations ---------------------------------------------------------

    async def send(self, channel: str, text: str, *, thread: str | None = None) -> dict:
        body = {"channel": channel, "text": text}
        if thread:
            body["thread"] = thread
        resp = await self._post(f"/v1/{self.platform}/send", body)
        return {
            "message_id": resp.get("message_id", ""),
            "channel_id": resp.get("channel_id", channel),
            "ts": resp.get("ts", ""),
        }

    async def list_channels(self, *, include_dms: bool = False) -> list[Channel]:
        params = {"include_dms": "true"} if include_dms else {}
        resp = await self._get(f"/v1/{self.platform}/channels", **params)
        return [Channel(id=c.get("id", ""), name=c.get("name", ""),
                        kind=c.get("kind", "")) for c in resp.get("channels", [])]

    async def open_dm(self, user: str) -> Channel:
        resp = await self._post(f"/v1/{self.platform}/open_dm", {"user": user})
        return Channel(id=resp.get("id", ""), name=resp.get("name", "") or user,
                       kind=resp.get("kind", "dm"))

    async def identity(self) -> Identity:
        resp = await self._get(f"/v1/{self.platform}/identity")
        return Identity(id=resp.get("id", ""), name=resp.get("name", ""))

<<<<<<< HEAD
    async def history(
=======
    async def fetch(
>>>>>>> feat/svc-social
        self, channel: str, *, limit: int = 50, before: str | None = None
    ) -> list[InboundMessage]:
        params: dict = {"channel": channel, "limit": limit}
        if before:
            params["before"] = before
        resp = await self._get(f"/v1/{self.platform}/messages", **params)
        msgs = resp.get("messages", [])  # daemon returns newest-first
        return [self._to_inbound(m) for m in reversed(msgs)]  # oldest->newest

<<<<<<< HEAD
=======
    async def search(
        self, query: str, *, limit: int = 50, channel: str | None = None
    ) -> list[InboundMessage]:
        params: dict = {"query": query, "limit": limit}
        if channel:
            params["channel"] = channel
        resp = await self._get(f"/v1/{self.platform}/search", **params)
        # Daemon ranks newest/most-relevant first; preserve that order.
        return [self._to_inbound(m) for m in resp.get("messages", [])]

    async def download_attachments(
        self, channel: str, message_id: str, *, idx: int | None = None
    ) -> list[tuple[str, str, bytes]]:
        import base64

        params: dict = {"channel": channel, "message_id": message_id}
        if idx is not None:
            params["idx"] = idx
        resp = await self._get(f"/v1/{self.platform}/download", **params)
        out: list[tuple[str, str, bytes]] = []
        for f in resp.get("files", []) or []:
            out.append((
                f.get("filename", "") or "",
                f.get("mime", "") or "",
                base64.b64decode(f.get("b64", "") or ""),
            ))
        return out

>>>>>>> feat/svc-social
    # -- inbound (WebSocket push) ------------------------------------------

    async def start(self) -> None:
        if not self._base or not self._token:
            log.error("mira[%s] missing url/token; account down", self.account.name)
            return
        try:
            import aiohttp  # noqa: F401
        except ImportError as exc:
            log.error("aiohttp not installed; mira[%s] disabled: %s",
                      self.account.name, exc)
            return
        try:
            ident = await self.identity()
            self._self_id = ident.id or ""
            log.info("mira[%s] %s session is %s (%s)", self.account.name,
                     self.platform, ident.name or self._self_id, self._self_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("mira[%s] identity probe failed (continuing): %s",
                        self.account.name, exc)

        backoff = 1.0
        while True:
            try:
                await self._run_events()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("mira[%s] events stream lost (%s); retry in %.1fs",
                            self.account.name, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, WS_RETRY_MAX_S)

    async def _run_events(self) -> None:
        import aiohttp

        sess = await self._ensure_session()
        url = self._base + "/v1/events"
        async with sess.ws_connect(url, heartbeat=30.0) as ws:
            self._ws = ws
            log.info("mira[%s] events stream connected", self.account.name)
            async for msg in ws:
                if msg.type is not aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    frame = msg.json()
                except Exception:  # noqa: BLE001
                    continue
                if frame.get("type") != "message":
                    continue
                await self._handle_inbound(frame.get("message") or {})

    def _to_inbound(self, m: dict) -> InboundMessage:
        """Map one daemon message dict to a normalised InboundMessage.

        Shared by the live events stream and the on-demand history fetch — both
        receive the daemon's normalised shape (see ``mira/mira_api/drivers.py``).
        """
        sender = m.get("sender_id", "") or ""
        return InboundMessage(
            account=self.account.name,
            platform=self.platform,
            channel_id=m.get("channel_id", ""),
            channel_name=m.get("channel_name", ""),
            thread_id=m.get("thread_id", "") or "",
            sender_id=sender,
            sender_name=m.get("sender_name", "") or sender,
            message_id=m.get("message_id", ""),
            ts=m.get("ts", ""),
            text=m.get("text", "") or "",
<<<<<<< HEAD
=======
            attachments=_to_attachments(m.get("attachments") or []),
>>>>>>> feat/svc-social
            raw=m,
        )

    async def _handle_inbound(self, m: dict) -> None:
        if m.get("platform") != self.platform:
            return  # daemon multiplexes all platforms; keep only ours
        sender = m.get("sender_id", "") or ""
        if sender and sender == self._self_id:
            return  # echo of our own send (daemon also filters, belt + braces)
        try:
            await self.on_message(self._to_inbound(m))
        except Exception as exc:  # noqa: BLE001 — one bad frame never kills the loop
            log.warning("mira[%s] inbound handler failed: %s",
                        self.account.name, exc)

    async def close(self) -> None:
        if self._ws is not None and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        self._ws = None
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
