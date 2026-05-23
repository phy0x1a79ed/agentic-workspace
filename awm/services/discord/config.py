"""Discord bot config.

Lives at ``$AWM_DIR/discord.toml`` (workspace-local, mode 0600). Shape::

    [discord]
    token = "..."          # Bot token from the Discord developer portal.
    account_name = "..."   # Optional; for logging.

Returning ``None`` means "no config" — the bot is opt-in and the
listener still starts fine without it.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from awm import config


CONFIG_FILE = config.AWM_DIR / "discord.toml"


@dataclass(frozen=True)
class DiscordConfig:
    token: str
    account_name: str | None = None


class DiscordConfigError(Exception):
    pass


def load() -> DiscordConfig | None:
    """Load and validate ``$AWM_DIR/discord.toml``.

    Returns ``None`` if the file doesn't exist (bot disabled). Raises
    :class:`DiscordConfigError` if it exists but is malformed — better to
    fail loudly than silently disable the bot on a typo.
    """
    path: Path = CONFIG_FILE
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DiscordConfigError(f"could not parse {path}: {exc}") from exc

    section = data.get("discord")
    if not isinstance(section, dict):
        raise DiscordConfigError(
            f"{path} missing [discord] section"
        )
    token = section.get("token")
    if not isinstance(token, str) or not token.strip():
        raise DiscordConfigError(
            f"{path}: [discord].token must be a non-empty string"
        )
    account_name = section.get("account_name")
    if account_name is not None and not isinstance(account_name, str):
        raise DiscordConfigError(
            f"{path}: [discord].account_name must be a string when present"
        )
    return DiscordConfig(
        token=token.strip(),
        account_name=account_name.strip() if account_name else None,
    )
