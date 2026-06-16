"""virtual-auth relay for the awm Discord bot.

The virtual-auth daemon on mira posts login notices to the unimatrix0 channel
*as this bot* (via REST). Concurrent-login notices carry Approve/Deny buttons
with custom_ids ``va:approve:<urgid>`` / ``va:deny:<urgid>``; we also expose an
``/approve-all`` slash command. Because those messages are authored by this bot's
application, Discord routes the button clicks to this gateway session — we handle
them here and relay the decision to the mira daemon over SSH:

    ssh <host> -- <bin> ctl <approve|deny|approve-all> [urgid]

Only whitelisted awm operators may drive it. Plug in from ``bot.py`` with::

    from awm.services.discord import virtual_auth_relay
    virtual_auth_relay.register(client, tree)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

from awm.services.discord import operators

log = logging.getLogger("awm.discord.virtual_auth")

SSH_HOST = os.environ.get("VA_SSH_HOST", "mira")
VA_BIN = os.environ.get("VA_BIN", "~/virtual-auth/.venv/bin/virtual-auth")
CUSTOM_ID_RE = re.compile(r"^va:(approve|deny):(?P<urgid>[A-Za-z0-9_-]{1,128})$")


async def _relay(*args: str) -> tuple[bool, str]:
    """Run ``virtual-auth ctl <args>`` on mira. Returns (ok, output)."""
    cmd = ["ssh", SSH_HOST, "--", VA_BIN, "ctl", *args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        text = (out or b"").decode("utf-8", "replace").strip()
        ok = proc.returncode == 0 and text.startswith("OK")
        return ok, text or f"(exit {proc.returncode})"
    except Exception as e:  # noqa: BLE001
        log.exception("relay failed")
        return False, f"relay error: {e}"


def register(client, tree) -> None:
    import discord
    from discord import app_commands

    def _authorized(interaction) -> bool:
        return operators.lookup(str(interaction.user.id)) is not None

    @tree.command(name="approve-all",
                  description="virtual-auth: approve all Duo logins for 5 minutes.")
    async def approve_all_cmd(interaction: discord.Interaction):
        if not _authorized(interaction):
            await interaction.response.send_message("not authorized.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, text = await _relay("approve-all")
        await interaction.followup.send(("✅ " if ok else "⚠️ ") + text, ephemeral=True)

    @client.event
    async def on_interaction(interaction: discord.Interaction):
        # App-command interactions are handled by the CommandTree automatically;
        # we only care about our own button components here.
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")
        m = CUSTOM_ID_RE.match(custom_id)
        if not m:
            return  # not ours
        if not _authorized(interaction):
            await interaction.response.send_message("not authorized.", ephemeral=True)
            return
        action = custom_id.split(":")[1]
        urgid = m.group("urgid")
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, text = await _relay(action, urgid)
        who = interaction.user.display_name
        # Update the original burst message so everyone sees the outcome.
        try:
            verb = "approved" if action == "approve" else "denied"
            await interaction.message.edit(
                content=f"{'✅' if ok and action=='approve' else '⛔'} {verb} by {who}",
                embeds=interaction.message.embeds, view=None,
            )
        except Exception:  # noqa: BLE001
            pass
        await interaction.followup.send(("✅ " if ok else "⚠️ ") + text, ephemeral=True)

    log.info("virtual-auth relay registered (ssh %s)", SSH_HOST)
