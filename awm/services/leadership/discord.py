"""Discord gateway lifecycle hooked to leadership state.

Shape A (single shared bot token across peers): every peer's
``$AWM_DIR/discord.toml`` carries the same token. Discord enforces one
gateway per token, so only the ACTIVE peer's connection should be open. We
make this explicit by starting the bot task on STANDBY→ACTIVE and cancelling
it on ACTIVE→STANDBY, with a small reconnect backoff so transient flap
between two peers doesn't crash either task.
"""

from __future__ import annotations

import asyncio
import logging

from awm.services.discord import config as discord_config

log = logging.getLogger("awm.leadership.discord")

RECONNECT_BACKOFF_S = 30.0

_task: asyncio.Task | None = None
_cfg: discord_config.DiscordConfig | None = None


def set_config(cfg: discord_config.DiscordConfig | None) -> None:
    """Stash the loaded Discord config for use by the start callback. Called
    once from lifespan before leadership starts polling."""
    global _cfg
    _cfg = cfg


async def _bot_supervisor() -> None:
    """Run the bot; on graceful close (Discord-side conflict / network) wait
    RECONNECT_BACKOFF_S and try again. Cancellation propagates out."""
    from awm.services.discord import bot as discord_bot

    while True:
        try:
            assert _cfg is not None
            await discord_bot.run_bot(_cfg)
            log.info(
                "discord bot exited cleanly; will retry in %.0fs (likely "
                "another peer is holding the gateway)",
                RECONNECT_BACKOFF_S,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "discord bot crashed: %s — retry in %.0fs",
                exc, RECONNECT_BACKOFF_S,
            )
        await asyncio.sleep(RECONNECT_BACKOFF_S)


async def start_bot() -> None:
    """on_promote callback: spin up the gateway supervisor task."""
    global _task
    if _cfg is None:
        return  # no discord.toml on this host — bot stays off
    if _task is not None and not _task.done():
        return
    log.info("promotion: starting discord gateway task")
    _task = asyncio.create_task(_bot_supervisor(), name="discord-supervisor")


async def stop_bot() -> None:
    """on_demote callback: cancel the gateway task and wait for it."""
    global _task
    if _task is None:
        return
    log.info("demotion: stopping discord gateway task")
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    finally:
        _task = None
