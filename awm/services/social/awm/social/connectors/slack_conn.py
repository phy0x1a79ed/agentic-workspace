"""Slack connector — wraps ``slack_sdk`` for one account.

Send + channel listing go through :class:`~slack_sdk.web.async_client.AsyncWebClient`
(the ``token``: a bot ``xoxb-`` or user ``xoxp-`` OAuth token). Receive goes
through Socket Mode (:class:`~slack_sdk.socket_mode.aiohttp.SocketModeClient`),
which REQUIRES a separate app-level ``app_token`` (``xapp-``). With no
``app_token`` the connector still sends but logs a warning and never receives.

Both bot and user tokens are legitimate on Slack, so this connector serves the
``slack-bot`` and ``slack-me`` accounts identically.
"""

from __future__ import annotations

import asyncio
import logging

from awm.social.connectors.base import (
    Account, Channel, Connector, Identity, InboundMessage, OnMessage,
)

log = logging.getLogger("awm.social.connectors.slack")


class SlackConnector(Connector):
    platform = "slack"

    def __init__(self, account: Account, on_message: OnMessage) -> None:
        super().__init__(account, on_message)
        self._web = None       # AsyncWebClient (send / list)
        self._socket = None     # SocketModeClient (receive)
        self._self_id = ""      # our own user id, to drop echoes

    def _web_client(self):
        from slack_sdk.web.async_client import AsyncWebClient

        if self._web is None:
            self._web = AsyncWebClient(token=self.account.token)
        return self._web

    async def _handle_request(self, client, req) -> None:  # noqa: ANN001
        from slack_sdk.socket_mode.response import SocketModeResponse

        # Always ack first so Slack doesn't redeliver.
        if req.envelope_id:
            await client.send_socket_mode_response(
                SocketModeResponse(envelope_id=req.envelope_id))
        if req.type != "events_api":
            return
        event = (req.payload or {}).get("event") or {}
        if event.get("type") != "message":
            return
        # Skip message subtypes (edits, joins, bot echoes) and our own messages.
        if event.get("subtype"):
            return
        sender = event.get("user") or ""
        if sender and sender == self._self_id:
            return
        try:
            inbound = InboundMessage(
                account=self.account.name,
                platform=self.platform,
                channel_id=event.get("channel", ""),
                channel_name="",
                thread_id=event.get("thread_ts", "") or "",
                sender_id=sender,
                sender_name=sender,
                message_id=event.get("ts", ""),
                ts=event.get("ts", ""),
                text=event.get("text", "") or "",
            )
            await self.on_message(inbound)
        except Exception as exc:  # noqa: BLE001 — one bad event never kills the loop
            log.warning("slack[%s] event handler failed: %s",
                        self.account.name, exc)

    async def start(self) -> None:
        try:
            from slack_sdk.web.async_client import AsyncWebClient  # noqa: F401
        except ImportError as exc:
            log.error("slack_sdk not installed; slack[%s] disabled: %s",
                      self.account.name, exc)
            return

        # Resolve our own user id once so we can drop echoes of our own sends.
        try:
            auth = await self._web_client().auth_test()
            self._self_id = auth.get("user_id", "") or ""
            log.info("slack[%s] authenticated as %s",
                     self.account.name, auth.get("user", self._self_id))
        except Exception as exc:  # noqa: BLE001
            log.error("slack[%s] auth_test failed; account down: %s",
                      self.account.name, exc)
            return

        if not self.account.app_token:
            log.warning(
                "slack[%s] has no app_token (xapp-); send works but Socket Mode "
                "receive is disabled", self.account.name)
            # Send-only account: nothing to run a receive loop for. Park until
            # cancelled so the task stays alive and send() keeps working.
            await asyncio.Event().wait()
            return

        from slack_sdk.socket_mode.aiohttp import SocketModeClient

        backoff = 1.0
        while True:
            try:
                self._socket = SocketModeClient(
                    app_token=self.account.app_token,
                    web_client=self._web_client(),
                )
                self._socket.socket_mode_request_listeners.append(
                    self._handle_request)
                await self._socket.connect()
                log.info("slack[%s] Socket Mode connected", self.account.name)
                backoff = 1.0
                # connect() returns once the WS is up; the SDK keeps it alive.
                # Park until cancelled; a hard drop raises out of connect on the
                # next cycle and we reconnect with backoff.
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("slack[%s] Socket Mode lost (%s); retry in %.1fs",
                            self.account.name, exc, backoff)
                await self.close()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def send(
        self, channel: str, text: str, *, thread: str | None = None
    ) -> dict:
        web = self._web_client()
        kwargs = {"channel": channel, "text": text}
        if thread:
            kwargs["thread_ts"] = thread
        resp = await web.chat_postMessage(**kwargs)
        return {
            "message_id": resp.get("ts", ""),
            "channel_id": resp.get("channel", channel),
            "ts": resp.get("ts", ""),
        }

    async def list_channels(self) -> list[Channel]:
        web = self._web_client()
        resp = await web.conversations_list(
            types="public_channel,private_channel", limit=200)
        out: list[Channel] = []
        for ch in resp.get("channels", []):
            out.append(Channel(
                id=ch.get("id", ""),
                name=ch.get("name", ""),
                kind="channel",
            ))
        return out

    async def identity(self) -> Identity:
        web = self._web_client()
        auth = await web.auth_test()
        return Identity(
            id=auth.get("user_id", ""), name=auth.get("user", ""))

    async def close(self) -> None:
        if self._socket is not None:
            try:
                await self._socket.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("slack[%s] close error: %s", self.account.name, exc)
            self._socket = None
