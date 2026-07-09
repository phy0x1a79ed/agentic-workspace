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
    Account, Attachment, Channel, Command, Connector, Identity, InboundMessage,
    OnCommand, OnMessage,
)

log = logging.getLogger("awm.social.connectors.discord")


def _attachments_of(message) -> list[Attachment]:  # noqa: ANN001
    """Map a discord.py message's ``.attachments`` to normalised metadata.

    Discord CDN urls are time-limited signed links, so ``url`` here is advisory
    only — :meth:`DiscordConnector.download_attachments` re-fetches the message
    and reads each file live rather than trusting a stored url.
    """
    out: list[Attachment] = []
    for i, a in enumerate(getattr(message, "attachments", []) or []):
        out.append(Attachment(
            idx=i,
            filename=getattr(a, "filename", "") or f"attachment-{i}",
            mime=getattr(a, "content_type", "") or "",
            size=int(getattr(a, "size", 0) or 0),
            url=getattr(a, "url", "") or "",
            ref={"id": str(getattr(a, "id", ""))},
        ))
    return out

# Slash command exposed by the bot. Kept deliberately tiny — one command, used
# by DMing the bot, that other services react to via the adapter's ``command``
# emit. ``approve`` is consumed by the 2fa service to arm a Duo approval burst.
APPROVE_COMMAND = "approve"
# Duo devices a user can target. Mirrors the 2fa service's device names; offered
# as Discord choices so the slash UI is a pick-list rather than free text.
DEVICE_CHOICES = ("cwl", "alliance")


class DiscordConnector(Connector):
    platform = "discord"

    def __init__(self, account: Account, on_message: OnMessage,
                 on_command: OnCommand | None = None) -> None:
        super().__init__(account, on_message, on_command)
        self._client = None  # type: ignore[assignment]
        self._tree = None    # discord.app_commands.CommandTree
        self._ready = asyncio.Event()

    def _build_client(self):
        import discord
        from discord import app_commands

        intents = discord.Intents.default()
        # Privileged intent: required to read message text. Must be toggled on
        # in the developer portal too; if it isn't, discord.py raises at login
        # and we log it as a permanent failure.
        intents.message_content = True
        client = discord.Client(intents=intents)
        tree = app_commands.CommandTree(client)
        self._tree = tree
        self._register_commands(tree, app_commands)

        @client.event
        async def on_ready():  # noqa: ANN202
            self._ready.set()
            # Global sync: a global command is the only kind that shows up in a
            # DM with the bot (guild-scoped commands never do). First propagation
            # can lag (up to ~1h) but is usually quick; re-syncs are cheap no-ops.
            try:
                synced = await tree.sync()
                log.info("discord[%s] synced %d app command(s)",
                         self.account.name, len(synced))
            except Exception as exc:  # noqa: BLE001 — never kill the ready path
                log.warning("discord[%s] command sync failed: %s",
                            self.account.name, exc)
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
                    attachments=_attachments_of(message),
                )
                await self.on_message(inbound)
            except Exception as exc:  # noqa: BLE001 — one bad message never kills the loop
                log.warning("discord[%s] on_message handler failed: %s",
                            self.account.name, exc)

        return client

    def _register_commands(self, tree, app_commands) -> None:
        """Register the bot's slash command(s) on the command tree.

        One command — ``/approve [device]`` — usable only as a DM to the bot
        (``interaction.guild is None``). It acks within Discord's 3-second
        interaction deadline, then hands a normalised :class:`Command` to the
        injected ``on_command`` callback (the adapter emits it for the 2fa
        service to consume). The command surface stays here; the *meaning* of
        ``approve`` lives entirely on the 2fa side.
        """

        choices = [app_commands.Choice(name=d, value=d) for d in DEVICE_CHOICES]

        @tree.command(
            name=APPROVE_COMMAND,
            description="Auto-approve Duo logins for 5 minutes (DM the bot).",
        )
        @app_commands.describe(device="Duo device to approve for (default cwl)")
        @app_commands.choices(device=choices)
        async def approve(interaction, device: str | None = None):  # noqa: ANN001, ANN202
            # DM-only authorization: only honour invocations in a DM to the bot.
            if interaction.guild is not None:
                try:
                    await interaction.response.send_message(
                        "DM me to use this command.", ephemeral=True)
                except Exception as exc:  # noqa: BLE001
                    log.debug("discord[%s] guild-reject reply failed: %s",
                              self.account.name, exc)
                return
            # With a str param + choices, discord hands back the chosen value
            # directly (not a Choice object).
            dev = device or "cwl"
            # Ack first — the burst is armed asynchronously by the 2fa service.
            try:
                await interaction.response.send_message(
                    f"⏳ Arming `{dev}` Duo approvals for 5 min…")
            except Exception as exc:  # noqa: BLE001
                log.warning("discord[%s] approve ack failed: %s",
                            self.account.name, exc)
            if self.on_command is None:
                return
            user = getattr(interaction, "user", None)
            cmd = Command(
                account=self.account.name,
                platform=self.platform,
                name=APPROVE_COMMAND,
                options={"device": dev},
                user_id=str(getattr(user, "id", "")) if user else "",
                user_name=str(user) if user else "",
                channel_id=str(getattr(interaction, "channel_id", "") or ""),
                is_dm=True,
            )
            try:
                await self.on_command(cmd)
            except Exception as exc:  # noqa: BLE001 — never bubble into the dispatcher
                log.warning("discord[%s] on_command handler failed: %s",
                            self.account.name, exc)

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

    async def _resolve_channel(self, channel: str):
        """Resolve a channel id to a discord.py channel object (cached or fetched)."""
        target_id = int(channel)
        target = self._client.get_channel(target_id)
        if target is None:
            target = await self._client.fetch_channel(target_id)
        return target

    async def fetch(
        self, channel: str, *, limit: int = 50, before: str | None = None
    ) -> list[InboundMessage]:
        import discord

        if self._client is None:
            raise RuntimeError(f"discord[{self.account.name}] not started")
        await self._wait_ready()
        target = await self._resolve_channel(channel)
        kwargs: dict = {"limit": limit}
        if before:
            kwargs["before"] = discord.Object(id=int(before))
        out: list[InboundMessage] = []
        async for message in target.history(**kwargs):  # newest-first by default
            out.append(InboundMessage(
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
                attachments=_attachments_of(message),
            ))
        out.reverse()  # oldest->newest
        return out

    async def download_attachments(
        self, channel: str, message_id: str, *, idx: int | None = None
    ) -> list[tuple[str, str, bytes]]:
        # No stored urls: re-fetch the message so the CDN links are fresh, then
        # read each attachment's bytes in-session (discord.py signs the request).
        if self._client is None:
            raise RuntimeError(f"discord[{self.account.name}] not started")
        await self._wait_ready()
        target = await self._resolve_channel(channel)
        message = await target.fetch_message(int(message_id))
        out: list[tuple[str, str, bytes]] = []
        for i, a in enumerate(getattr(message, "attachments", []) or []):
            if idx is not None and i != idx:
                continue
            data = await a.read()
            out.append((getattr(a, "filename", "") or f"attachment-{i}",
                        getattr(a, "content_type", "") or "", data))
        return out

    # `search` is intentionally not implemented: a Discord *bot* token cannot
    # use the user message-search endpoint, so the ABC default raises a clean
    # "not supported" error. `fetch` covers per-channel history retrieval.

    async def list_channels(self, *, include_dms: bool = False) -> list[Channel]:
        import discord  # noqa: F401

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
        if include_dms:
            # Already-open DM channels the bot can see. (Discord opens a DM
            # lazily on first contact; this lists the ones that exist.)
            for dm in getattr(self._client, "private_channels", []):
                if getattr(dm, "type", None) is discord.ChannelType.private:
                    recipient = getattr(dm, "recipient", None)
                    out.append(Channel(
                        id=str(dm.id),
                        name=str(recipient) if recipient else f"dm:{dm.id}",
                        kind="dm",
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
