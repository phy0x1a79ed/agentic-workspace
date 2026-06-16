"""Discord bot — exposes ``/login`` to whitelisted operators.

On ``/login``, the bot:

  1. Looks up the invoking user in the ``discord_operators`` table.
     Absent → ephemeral "not authorized".
  2. Mints a one-shot login nonce via :func:`awm.services.auth.mint_challenge`.
  3. DMs the user the bootstrap URL.
  4. On DMBlocked, falls back to an ephemeral reply with the URL inline
     (with a warning that anyone seeing the screen can use it).

The bot runs as a background task on the exposed listener's asyncio
event loop. ``run_bot()`` is the public entry point; callers wrap it in
``asyncio.create_task`` and cancel on shutdown.
"""

from __future__ import annotations

import logging

from awm.services import auth as auth_svc
from awm.services.discord import config as discord_config
from awm.services.discord import operators

log = logging.getLogger("awm.discord")


async def run_bot(cfg: discord_config.DiscordConfig) -> None:
    """Connect to Discord and serve ``/login`` until cancelled.

    Late-imports ``discord`` so a missing dependency doesn't break
    daemon startup — the discord_config.load() returning None already
    gates this, but we want the import error path to be obvious if the
    config exists but the lib doesn't.
    """
    try:
        import discord
        from discord import app_commands
    except ImportError as exc:
        log.error("discord.py not installed; bot disabled: %s", exc)
        return

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    # virtual-auth: /approve-all + Approve/Deny button relay to the mira daemon.
    try:
        from awm.services.discord import virtual_auth_relay
        virtual_auth_relay.register(client, tree)
    except Exception as exc:  # noqa: BLE001 — never let the relay break /login
        log.warning("virtual-auth relay not registered: %s", exc)

    @tree.command(name="login", description="Get a one-shot awm sign-in link.")
    async def login_cmd(interaction: discord.Interaction):
        discord_uid = str(interaction.user.id)
        awm_user = operators.lookup(discord_uid)
        if awm_user is None:
            await interaction.response.send_message(
                "not authorized — ask the awm operator to "
                f"`awm discord add-operator {discord_uid} <awm_user>`",
                ephemeral=True,
            )
            return

        nonce = auth_svc.mint_challenge(awm_user)
        url = f"{auth_svc.base_url()}/auth/bootstrap?ot={nonce}"
        msg = (
            f"Click to sign in (expires in {auth_svc.CHALLENGE_TTL_SECONDS}s):\n"
            f"<{url}>"
        )
        try:
            channel = await interaction.user.create_dm()
            await channel.send(msg)
            await interaction.response.send_message(
                "DM sent — check your messages.", ephemeral=True,
            )
        except discord.Forbidden:
            # DMs disabled — fall back to ephemeral reply.
            await interaction.response.send_message(
                "Your DMs are closed; here is the link (visible only to you):\n"
                f"{msg}",
                ephemeral=True,
            )

    @client.event
    async def on_ready():
        try:
            await tree.sync()
        except Exception as exc:  # noqa: BLE001
            log.warning("slash-command sync failed: %s", exc)
        log.info("discord bot ready as %s", client.user)

    try:
        await client.start(cfg.token)
    finally:
        if not client.is_closed():
            await client.close()
