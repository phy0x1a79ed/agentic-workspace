"""Social service config — named accounts.

Lives at ``$AWM_DIR/social.toml`` (workspace-local, mode 0600). Each
``[account.<name>]`` section is one identity the service can act as: the
section name is the account id, and the body names a ``platform`` + its
token(s). Shape::

    [account.discord-bot]          # Discord is bot-only (user token = self-bot).
    platform = "discord"
    token = "..."                  # Bot token from the developer portal.

    [account.slack-bot]
    platform = "slack"
    token = "xoxb-..."             # Bot OAuth token.
    app_token = "xapp-..."         # App-level token; required for Socket Mode.

    [account.slack-me]             # Legitimate "me": a Slack user OAuth token.
    platform = "slack"
    token = "xoxp-..."
    app_token = "xapp-..."

Tokens live ONLY in this file — the DB and logs hold metadata only. Returning
an empty list means "no accounts": the service still boots with zero live
connections, since every connector is opt-in.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from awm import config


CONFIG_FILE = config.AWM_DIR / "social.toml"

# Platforms this build knows how to connect. The config loader validates
# against it so a typo'd platform fails loudly instead of silently never
# connecting. Keep in sync with ``connectors.REGISTRY``.
KNOWN_PLATFORMS = ("discord", "slack")


@dataclass(frozen=True)
class AccountConfig:
    """One configured identity. ``name`` is the account id (section name)."""

    name: str
    platform: str
    token: str
    app_token: str | None = None
    display_name: str | None = None
    enabled: bool = True

    @property
    def kind(self) -> str:
        """Best-effort "bot" vs "user" label, derived from the token shape.

        Slack user OAuth tokens start ``xoxp-``; everything else (Slack bot
        ``xoxb-``, Discord bot tokens) is treated as a bot identity. This is a
        display hint only — never an authz decision.
        """
        if self.token.startswith("xoxp-"):
            return "user"
        return "bot"


class SocialConfigError(Exception):
    pass


def load(path: Path | None = None) -> list[AccountConfig]:
    """Load and validate ``$AWM_DIR/social.toml`` into a list of accounts.

    Returns ``[]`` if the file doesn't exist (no accounts configured). Raises
    :class:`SocialConfigError` if it exists but is malformed — better to fail
    loudly than silently drop an account on a typo.
    """
    path = path if path is not None else CONFIG_FILE
    if not path.exists():
        return []
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SocialConfigError(f"could not parse {path}: {exc}") from exc

    accounts_tbl = data.get("account")
    if accounts_tbl is None:
        return []
    if not isinstance(accounts_tbl, dict):
        raise SocialConfigError(
            f"{path}: [account] must be a table of named accounts"
        )

    accounts: list[AccountConfig] = []
    for name, section in accounts_tbl.items():
        if not isinstance(section, dict):
            raise SocialConfigError(
                f"{path}: [account.{name}] must be a table"
            )
        platform = section.get("platform")
        if not isinstance(platform, str) or platform not in KNOWN_PLATFORMS:
            raise SocialConfigError(
                f"{path}: [account.{name}].platform must be one of "
                f"{', '.join(KNOWN_PLATFORMS)}"
            )
        token = section.get("token")
        if not isinstance(token, str) or not token.strip():
            raise SocialConfigError(
                f"{path}: [account.{name}].token must be a non-empty string"
            )
        app_token = section.get("app_token")
        if app_token is not None and not isinstance(app_token, str):
            raise SocialConfigError(
                f"{path}: [account.{name}].app_token must be a string when present"
            )
        display_name = section.get("display_name")
        if display_name is not None and not isinstance(display_name, str):
            raise SocialConfigError(
                f"{path}: [account.{name}].display_name must be a string when present"
            )
        enabled = section.get("enabled", True)
        if not isinstance(enabled, bool):
            raise SocialConfigError(
                f"{path}: [account.{name}].enabled must be a boolean"
            )
        accounts.append(AccountConfig(
            name=str(name).strip(),
            platform=platform,
            token=token.strip(),
            app_token=app_token.strip() if app_token else None,
            display_name=display_name.strip() if display_name else None,
            enabled=enabled,
        ))
    return accounts
