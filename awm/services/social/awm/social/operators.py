"""Public CRUD over the ``social_operators`` table.

Thin function-level wrappers over :class:`awm.social.dao.SocialDAO`, so handler
and connector code can ``from awm.social.operators import lookup`` without
threading a DAO instance around. Each ensures the DB exists (idempotent
``init``) and delegates to a shared DAO instance.

A row platform-scopes an allowlist: a ``(platform, platform_user_id)`` maps to
an ``awm_user`` identity. Generalises the old ``discord_operators``.
"""

from __future__ import annotations

from awm.social.dao import SocialDAO, init

_dao = SocialDAO()


def add_operator(platform: str, platform_user_id: str, awm_user: str) -> dict:
    """Idempotent: re-adding overwrites the ``awm_user`` mapping."""
    init()
    return _dao.add_operator(platform, platform_user_id, awm_user)


def remove_operator(platform: str, platform_user_id: str) -> bool:
    """Return True if a row was deleted."""
    init()
    return _dao.remove_operator(platform, platform_user_id)


def list_operators(platform: str | None = None) -> list[dict]:
    init()
    return _dao.list_operators(platform)


def lookup(platform: str, platform_user_id: str) -> str | None:
    """Return the ``awm_user`` for a (platform, user id), or None."""
    init()
    return _dao.lookup(platform, platform_user_id)
