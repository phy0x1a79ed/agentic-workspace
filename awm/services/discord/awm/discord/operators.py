"""Public CRUD over the ``discord_operators`` table.

Thin function-level wrappers over :class:`awm.discord.dao.DiscordDAO`, kept so
bot code can ``from awm.discord.operators import add_operator`` as before. The
table now lives in the discord service's own DB (see ``dao.py``); these helpers
ensure it exists (idempotent ``init``) and delegate to a shared DAO instance.

A row whitelists a Discord user to run the ``/login`` slash command and maps
them to an ``awm_user`` identity that ``X-Awm-As`` will report.
"""

from __future__ import annotations

from awm.discord.dao import DiscordDAO, init

_dao = DiscordDAO()


def add_operator(discord_user_id: str, awm_user: str) -> dict:
    """Idempotent: re-adding overwrites the ``awm_user`` mapping."""
    init()
    return _dao.add_operator(discord_user_id, awm_user)


def remove_operator(discord_user_id: str) -> bool:
    """Return True if a row was deleted."""
    init()
    return _dao.remove_operator(discord_user_id)


def list_operators() -> list[dict]:
    init()
    return _dao.list_operators()


def lookup(discord_user_id: str) -> str | None:
    """Return the ``awm_user`` for a Discord ID, or None if not whitelisted."""
    init()
    return _dao.lookup(discord_user_id)
