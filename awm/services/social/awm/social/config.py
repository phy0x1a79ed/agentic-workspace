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

    [account.slack-mira]           # Slack web-session "me", creds pulled live from
    platform = "slack"             # a logged-in client on the mira host (durable;
    creds_cmd = "ssh mira /home/tony/.local/bin/awm-slack-creds"  # never on disk).

A web-session account may instead pin the pair inline (``token = "xoxc-..."`` +
``cookie = "xoxd-..."``, or ``cookie_file``) — both are required together. The
``creds_cmd`` form fetches the ``xoxc-`` token and ``d`` cookie at runtime, so
nothing secret is stored. Tokens live ONLY in this file — the DB and logs hold metadata only. Returning
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
KNOWN_PLATFORMS = ("discord", "slack", "gmail")


@dataclass(frozen=True)
class AccountConfig:
    """One configured identity. ``name`` is the account id (section name)."""

    name: str
    platform: str
    token: str
    app_token: str | None = None
    cookie: str | None = None     # Slack session 'd' cookie (xoxc session mode)
    creds_cmd: str | None = None  # command printing {token,cookie} JSON (mira pull)
    display_name: str | None = None
    address: str | None = None  # mailbox/login address (required for gmail)
    enabled: bool = True

    @property
    def kind(self) -> str:
        """Best-effort "bot" vs "user" label, derived from platform + token.

        Gmail (an App-Password mailbox), Slack user OAuth tokens (``xoxp-``), and
        Slack web-session tokens (``xoxc-``, including the ``creds_cmd`` pull mode
        where the token is fetched at runtime) are "user" identities; everything
        else (Slack bot ``xoxb-``, Discord bot tokens) is a "bot". This is a
        display hint only — never an authz decision.
        """
        if self.platform == "gmail" or self.token.startswith(("xoxp-", "xoxc-")):
            return "user"
        if self.platform == "slack" and self.creds_cmd:
            return "user"  # mira session pull — token is xoxc-, fetched live
        return "bot"


class SocialConfigError(Exception):
    pass


def _resolve_secret(
    inline: object, file_ref: object, cfg_path: Path, label: str,
    *, required: bool = True,
) -> str | None:
    """Resolve a secret from an inline value or a ``*_file`` path reference.

    ``token``/``app_token`` may be given inline, OR via ``token_file`` /
    ``app_token_file`` naming a file whose contents are the secret — so the
    secret can live in one gitignored file instead of being inlined into
    ``social.toml``. A relative ``*_file`` is resolved against the config file's
    directory. File contents have ALL whitespace stripped (tokens never contain
    any), so a Google App Password pasted with its display spaces still works.
    Returns ``None`` when neither is given and ``required`` is False.
    """
    if inline is not None:
        if not isinstance(inline, str) or not inline.strip():
            raise SocialConfigError(f"{cfg_path}: {label} must be a non-empty string")
        return inline.strip()
    if file_ref is not None:
        if not isinstance(file_ref, str) or not file_ref.strip():
            raise SocialConfigError(f"{cfg_path}: {label}_file must be a path string")
        p = Path(file_ref.strip())
        if not p.is_absolute():
            p = cfg_path.parent / p
        try:
            raw = p.read_text()
        except OSError as exc:
            raise SocialConfigError(
                f"{cfg_path}: {label}_file {p} could not be read: {exc}") from exc
        secret = "".join(raw.split())
        if not secret:
            raise SocialConfigError(f"{cfg_path}: {label}_file {p} is empty")
        return secret
    if required:
        raise SocialConfigError(
            f"{cfg_path}: {label} (or {label}_file) is required")
    return None


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
        creds_cmd = section.get("creds_cmd")
        if creds_cmd is not None and (
            not isinstance(creds_cmd, str) or not creds_cmd.strip()
        ):
            raise SocialConfigError(
                f"{path}: [account.{name}].creds_cmd must be a non-empty string"
            )
        creds_cmd = creds_cmd.strip() if creds_cmd else None
        # With creds_cmd the token (and cookie) are fetched live at runtime, so a
        # static token in the file is optional.
        token = _resolve_secret(
            section.get("token"), section.get("token_file"),
            path, f"[account.{name}].token",
            required=creds_cmd is None) or ""
        app_token = _resolve_secret(
            section.get("app_token"), section.get("app_token_file"),
            path, f"[account.{name}].app_token", required=False)
        cookie = _resolve_secret(
            section.get("cookie"), section.get("cookie_file"),
            path, f"[account.{name}].cookie", required=False)
        # Slack web-session mode: an inline xoxc- token needs its paired 'd'
        # cookie (neither works alone). The creds_cmd path supplies both at
        # runtime, so it's exempt.
        if (
            platform == "slack" and not creds_cmd
            and token.startswith("xoxc-") and not cookie
        ):
            raise SocialConfigError(
                f"{path}: [account.{name}] an xoxc- session token requires a "
                f"paired cookie (or cookie_file), or use creds_cmd"
            )
        display_name = section.get("display_name")
        if display_name is not None and not isinstance(display_name, str):
            raise SocialConfigError(
                f"{path}: [account.{name}].display_name must be a string when present"
            )
        address = section.get("address")
        if address is not None and not isinstance(address, str):
            raise SocialConfigError(
                f"{path}: [account.{name}].address must be a string when present"
            )
        if platform == "gmail" and (not address or not address.strip()):
            raise SocialConfigError(
                f"{path}: [account.{name}].address (the mailbox email) is "
                f"required for gmail accounts"
            )
        enabled = section.get("enabled", True)
        if not isinstance(enabled, bool):
            raise SocialConfigError(
                f"{path}: [account.{name}].enabled must be a boolean"
            )
        accounts.append(AccountConfig(
            name=str(name).strip(),
            platform=platform,
            token=token,            # already resolved + cleaned (or "" w/ creds_cmd)
            app_token=app_token,    # resolved (or None)
            cookie=cookie,          # Slack 'd' cookie (or None)
            creds_cmd=creds_cmd,    # mira pull command (or None)
            display_name=display_name.strip() if display_name else None,
            address=address.strip() if address else None,
            enabled=enabled,
        ))
    return accounts
