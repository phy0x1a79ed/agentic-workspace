"""Discord connector — wraps ``discord.py`` for one bot account.

Bot token ONLY. A Discord *user* token is a self-bot, violates Discord ToS, and
is bannable — that path does not exist here. To receive message text the bot's
"Message Content Intent" must be enabled in the Discord developer portal;
without it events still fire but ``text`` arrives empty.

``discord.py``'s :meth:`discord.Client.start` already owns reconnect/backoff, so
:meth:`start` simply awaits it and absorbs terminal errors rather than letting
them bubble into the adapter's control-WS loop.
"""

from __future__ import annotations

import asyncio
import logging

from awm.social.connectors.base import (
    Account, Channel, Connector, Identity, InboundMessage, OnMessage,
)

log = logging.getLogger("awm.social.connectors.discord")


class DiscordConnector(Connector):
    platform = "discord"

    def __init__(self, account: Account, on_message: OnMessage) -> None:
        super().__init__(account, on_message)
        self._client = None  # type: ignore[assignment]
        self._ready = asyncio.Event()

    def _build_client(self):
        import discord

        intents = discord.Intents.default()
        # Privileged intent: required to read message text. Must be toggled on
        # in the developer portal too; if it isn't, discord.py raises at login
        # and we log it as a permanent failure.
        intents.message_content = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready():  # noqa: ANN202
            self._ready.set()
            log.info("discord[%s] ready as %s", self.account.name, client.user)

        @client.event
        async def on_message(message):  # noqa: ANN001, ANN202
            # Ignore our own outbound messages so send→receive doesn't loop.
            if client.user is not None and message.author.id == client.user.id:
                return
            try:
                inbound = InboundMessage(
                    account=self.account.name,
                    platform=self.platform,
                    channel_id=str(message.channel.id),
                    channel_name=getattr(message.channel, "name", "") or "",
                    thread_id="",
                    sender_id=str(message.author.id),
                    sender_name=str(message.author),
                    message_id=str(message.id),
                    ts=message.created_at.isoformat() if message.created_at else "",
                    text=message.content or "",
                )
                await self.on_message(inbound)
            except Exception as exc:  # noqa: BLE001 — one bad message never kills the loop
                log.warning("discord[%s] on_message handler failed: %s",
                            self.account.name, exc)

        return client

    async def start(self) -> None:
        try:
            import discord  # noqa: F401
        except ImportError as exc:
            log.error("discord.py not installed; discord[%s] disabled: %s",
                      self.account.name, exc)
            return
        self._client = self._build_client()
        try:
            # client.start owns reconnect/backoff internally.
            await self._client.start(self.account.token)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # LoginFailure (bad token / missing privileged intent) and other
            # terminal errors land here. Log and return — do NOT re-raise into
            # the adapter loop; the account is simply down.
            log.error("discord[%s] connection ended: %s",
                      self.account.name, exc)
        finally:
            await self.close()

    async def _wait_ready(self, timeout: float = 30.0) -> None:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"discord[{self.account.name}] not connected") from exc

    async def send(
        self, channel: str, text: str, *, thread: str | None = None
    ) -> dict:
        import discord

        if self._client is None:
            raise RuntimeError(f"discord[{self.account.name}] not started")
        await self._wait_ready()
        target_id = int(thread or channel)
        target = self._client.get_channel(target_id)
        if target is None:
            target = await self._client.fetch_channel(target_id)
        sent = await target.send(text)
        return {
            "message_id": str(sent.id),
            "channel_id": str(sent.channel.id),
            "ts": sent.created_at.isoformat() if sent.created_at else "",
        }

    async def list_channels(self) -> list[Channel]:
        import discord

        if self._client is None:
            raise RuntimeError(f"discord[{self.account.name}] not started")
        await self._wait_ready()
        out: list[Channel] = []
        for guild in self._client.guilds:
            for ch in guild.text_channels:
                out.append(Channel(
                    id=str(ch.id),
                    name=f"{guild.name}#{ch.name}",
                    kind="text",
                ))
        return out

    async def identity(self) -> Identity:
        if self._client is None:
            raise RuntimeError(f"discord[{self.account.name}] not started")
        await self._wait_ready()
        u = self._client.user
        return Identity(id=str(u.id) if u else "", name=str(u) if u else "")

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed():
            try:
                await self._client.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("discord[%s] close error: %s", self.account.name, exc)
