"""Social service config — named accounts.

Lives at ``$AWM_DIR/social.toml`` (workspace-local, mode 0600). Each
``[account.<name>]`` section is one identity the service can act as: the
section name is the account id, and the body names a ``platform`` + its
token(s). Shape::

    [account.discord-bot]          # Discord is bot-only (user token = self-bot).
    platform = "discord"
    token = "..."                  # Bot token from the developer portal.
    singleton = true               # One session fleet-wide; see below.

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

    [mira]                         # The mira API daemon endpoint (REST + WS over
    url = "https://172.16.0.24:7822"   # the awm-network); drives the logged-in
    token_file = "~/.awm/auth.token"   # Slack/Teams web sessions on the mira host.
    verify_tls = false                 # self-signed cert by default

    [account.slack-via-mira]       # Full Slack ops routed through the mira daemon.
    platform = "slack"
    source = "mira"

    [account.teams]                # Teams is ONLY reachable via the mira daemon.
    platform = "teams"
    source = "mira"

``singleton = true`` marks an identity that is ONE session for the whole fleet —
a Discord bot token, a mailbox — where a second login is not a second client but
a collision (Discord hands interactions to one session, so the other answers with
"Unknown interaction"). The account's owner is whichever node leaves
``AWM_SOCIAL_PEER`` unset. A node that sets the selector does not connect such an
account at all; a verb naming it is forwarded to the peer instead, so the identity
stays usable from every node while existing exactly once. Non-singleton accounts
are per-node and unaffected.

A web-session account may instead pin the pair inline (``token = "xoxc-..."`` +
``cookie = "xoxd-..."``, or ``cookie_file``) — both are required together. The
``creds_cmd`` form fetches the ``xoxc-`` token and ``d`` cookie at runtime, so
nothing secret is stored. Tokens live ONLY in this file — the DB and logs hold metadata only. Returning
an empty list means "no accounts": the service still boots with zero live
connections, since every connector is opt-in.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from awm import config


# Default config path. ``AWM_SOCIAL_CONFIG`` overrides it wholesale — needed
# under ``awm dev shadow``, which rewrites ``AWM_WORKSPACE`` to a sandbox dir
# that doesn't hold the real ``social.toml`` (the same shape as the 2fa
# service's ``AWM_2FA_DIR`` override). Resolved at load() time, not import.
CONFIG_FILE = config.AWM_DIR / "social.toml"


def _config_file() -> Path:
    override = os.environ.get("AWM_SOCIAL_CONFIG")
    if override and override.strip():
        return Path(override.strip())
    return config.AWM_DIR / "social.toml"

# Platforms this build knows how to connect. The config loader validates
# against it so a typo'd platform fails loudly instead of silently never
# connecting. Keep in sync with ``connectors.REGISTRY`` (``teams`` has no native
# connector — it is only reachable via ``source = "mira"``).
KNOWN_PLATFORMS = ("discord", "slack", "gmail", "teams")

# Platforms that can ONLY be driven through the mira API daemon (no native
# connector exists), so an account on one of these must set ``source = "mira"``.
MIRA_ONLY_PLATFORMS = ("teams",)

# Cloud file-store backends this build knows how to drive. ``[bucket.<name>]``
# sections validate against this. ``onedrive`` is mira-only (SharePoint is driven
# through the logged-in mira session, exactly like ``teams``).
KNOWN_BUCKET_KINDS = ("google_drive", "onedrive")
MIRA_ONLY_BUCKET_KINDS = ("onedrive",)


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
    # Fleet singleton: this identity is ONE session shared by the whole fleet
    # (a Discord bot token, a mailbox), so exactly one node may connect it. The
    # owner is whichever node leaves AWM_SOCIAL_PEER unset; a node that sets the
    # selector treats the account as remote and forwards calls naming it. See
    # `_is_borrowed` in hub_adapter.py.
    singleton: bool = False
    # mira-routed accounts: the connector is a thin client of the mira API
    # daemon. These are stamped from the service-level [mira] block at load time.
    source: str | None = None       # "mira" routes via MiraConnector
    mira_url: str | None = None
    mira_token: str | None = None
    mira_verify_tls: bool = False

    @property
    def kind(self) -> str:
        """Best-effort "bot" vs "user" label, derived from platform + token.

        mira-routed accounts act as the logged-in *user* on mira; Gmail (an
        App-Password mailbox), Slack user OAuth tokens (``xoxp-``), and Slack
        web-session tokens (``xoxc-``, including the ``creds_cmd`` pull mode where
        the token is fetched at runtime) are "user" identities; everything else
        (Slack bot ``xoxb-``, Discord bot tokens) is a "bot". This is a display
        hint only — never an authz decision.
        """
        if self.source == "mira":
            return "user"
        if self.platform == "gmail" or self.token.startswith(("xoxp-", "xoxc-")):
            return "user"
        if self.platform == "slack" and self.creds_cmd:
            return "user"  # mira session pull — token is xoxc-, fetched live
        return "bot"


@dataclass(frozen=True)
class BucketConfig:
    """One configured cloud file store. ``name`` is the bucket id (section name).

    The storage-shaped sibling of :class:`AccountConfig`. A ``google_drive``
    bucket carries the OAuth2 triple (``client_id``/``client_secret``/
    ``refresh_token``); a ``onedrive`` bucket is mira-routed (``source = "mira"``
    + a SharePoint ``site`` origin) and the ``mira_*`` fields are stamped from the
    service-level ``[mira]`` block at load time, exactly as for mira accounts.
    ``root`` scopes the bucket to a sub-tree (a Drive folder id / a SharePoint
    server-relative path).
    """

    name: str
    kind: str
    root: str = ""
    client_id: str | None = None
    client_secret: str | None = None
    refresh_token: str | None = None
    source: str | None = None
    site: str | None = None
    mira_url: str | None = None
    mira_token: str | None = None
    mira_verify_tls: bool = False


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


def _load_mira(data: dict, path: Path) -> dict | None:
    """Parse the service-level ``[mira]`` block (the mira API daemon endpoint).

    Shape::

        [mira]
        url = "https://172.16.0.24:7822"
        token_file = "~/.awm/auth.token"   # or inline token = "..."
        verify_tls = false                 # self-signed cert by default

    Returns ``{url, token, verify_tls}`` or ``None`` when absent. The token is
    required (mira authenticates every request); ``~`` in ``token_file`` is
    expanded so the shared awm bearer file can be referenced directly.
    """
    section = data.get("mira")
    if section is None:
        return None
    if not isinstance(section, dict):
        raise SocialConfigError(f"{path}: [mira] must be a table")
    url = section.get("url")
    if not isinstance(url, str) or not url.strip():
        raise SocialConfigError(f"{path}: [mira].url is required")
    token_file = section.get("token_file")
    if isinstance(token_file, str) and token_file.strip().startswith("~"):
        section = {**section, "token_file": str(Path(token_file.strip()).expanduser())}
    token = _resolve_secret(
        section.get("token"), section.get("token_file"), path, "[mira].token")
    verify = section.get("verify_tls", False)
    if not isinstance(verify, bool):
        raise SocialConfigError(f"{path}: [mira].verify_tls must be a boolean")
    return {"url": url.strip(), "token": token, "verify_tls": verify}


def load(path: Path | None = None) -> list[AccountConfig]:
    """Load and validate ``$AWM_DIR/social.toml`` into a list of accounts.

    Returns ``[]`` if the file doesn't exist (no accounts configured). Raises
    :class:`SocialConfigError` if it exists but is malformed — better to fail
    loudly than silently drop an account on a typo.
    """
    path = path if path is not None else _config_file()
    if not path.exists():
        return []
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SocialConfigError(f"could not parse {path}: {exc}") from exc

    mira = _load_mira(data, path)

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
        source = section.get("source")
        if source is not None and source != "mira":
            raise SocialConfigError(
                f"{path}: [account.{name}].source must be \"mira\" when set"
            )
        if platform in MIRA_ONLY_PLATFORMS and source != "mira":
            raise SocialConfigError(
                f"{path}: [account.{name}] platform {platform!r} is only reachable "
                f"via source = \"mira\""
            )
        if source == "mira" and mira is None:
            raise SocialConfigError(
                f"{path}: [account.{name}] uses source = \"mira\" but no [mira] "
                f"block is configured"
            )
        creds_cmd = section.get("creds_cmd")
        if creds_cmd is not None and (
            not isinstance(creds_cmd, str) or not creds_cmd.strip()
        ):
            raise SocialConfigError(
                f"{path}: [account.{name}].creds_cmd must be a non-empty string"
            )
        creds_cmd = creds_cmd.strip() if creds_cmd else None
        # With creds_cmd the token (and cookie) are fetched live at runtime, and a
        # mira-routed account holds no platform token at all (the daemon owns the
        # live session), so a static token in the file is optional in both cases.
        token = _resolve_secret(
            section.get("token"), section.get("token_file"),
            path, f"[account.{name}].token",
            required=creds_cmd is None and source != "mira") or ""
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
        singleton = section.get("singleton", False)
        if not isinstance(singleton, bool):
            raise SocialConfigError(
                f"{path}: [account.{name}].singleton must be a boolean"
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
            singleton=singleton,
            source=source,
            mira_url=(mira or {}).get("url") if source == "mira" else None,
            mira_token=(mira or {}).get("token") if source == "mira" else None,
            mira_verify_tls=bool((mira or {}).get("verify_tls"))
            if source == "mira" else False,
        ))
    return accounts


def load_buckets(path: Path | None = None) -> list[BucketConfig]:
    """Load and validate the ``[bucket.<name>]`` sections of ``social.toml``.

    Separate from :func:`load` on purpose: bucket parsing is fully isolated from
    account parsing so a malformed ``[bucket]`` can never take down the messaging
    accounts (and vice-versa). Returns ``[]`` when the file or the ``[bucket]``
    table is absent. Each section names a ``kind`` (one of
    :data:`KNOWN_BUCKET_KINDS`):

        [bucket.gdrive-phyber]         # Google Drive via an OAuth2 refresh token.
        kind = "google_drive"
        client_id = "...apps.googleusercontent.com"
        client_secret_file = "gdrive-phyber.secret"   # or inline client_secret
        refresh_token_file = "gdrive-phyber.token"     # or inline refresh_token
        # root = "<folder-id>"        # optional: scope to a Drive folder

        [bucket.onedrive-ubc]          # UBC SharePoint via the mira session.
        kind = "onedrive"
        source = "mira"
        site = "https://ubcca.sharepoint.com"
        root = "/teams/.../Shared Documents/Hallam_Lab_roadmaps/Tony-L_-roadmap-_"
    """
    path = path if path is not None else _config_file()
    if not path.exists():
        return []
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SocialConfigError(f"could not parse {path}: {exc}") from exc

    mira = _load_mira(data, path)

    buckets_tbl = data.get("bucket")
    if buckets_tbl is None:
        return []
    if not isinstance(buckets_tbl, dict):
        raise SocialConfigError(
            f"{path}: [bucket] must be a table of named buckets")

    buckets: list[BucketConfig] = []
    for name, section in buckets_tbl.items():
        if not isinstance(section, dict):
            raise SocialConfigError(f"{path}: [bucket.{name}] must be a table")
        kind = section.get("kind")
        if not isinstance(kind, str) or kind not in KNOWN_BUCKET_KINDS:
            raise SocialConfigError(
                f"{path}: [bucket.{name}].kind must be one of "
                f"{', '.join(KNOWN_BUCKET_KINDS)}")
        source = section.get("source")
        if source is not None and source != "mira":
            raise SocialConfigError(
                f"{path}: [bucket.{name}].source must be \"mira\" when set")
        if kind in MIRA_ONLY_BUCKET_KINDS and source != "mira":
            raise SocialConfigError(
                f"{path}: [bucket.{name}] kind {kind!r} is only reachable via "
                f"source = \"mira\"")
        if source == "mira" and mira is None:
            raise SocialConfigError(
                f"{path}: [bucket.{name}] uses source = \"mira\" but no [mira] "
                f"block is configured")
        root = section.get("root")
        if root is not None and not isinstance(root, str):
            raise SocialConfigError(
                f"{path}: [bucket.{name}].root must be a string when present")
        site = section.get("site")
        if site is not None and not isinstance(site, str):
            raise SocialConfigError(
                f"{path}: [bucket.{name}].site must be a string when present")

        client_id = None
        client_secret = None
        refresh_token = None
        if kind == "google_drive":
            client_id = section.get("client_id")
            if not isinstance(client_id, str) or not client_id.strip():
                raise SocialConfigError(
                    f"{path}: [bucket.{name}].client_id is required for "
                    f"google_drive buckets")
            client_id = client_id.strip()
            client_secret = _resolve_secret(
                section.get("client_secret"), section.get("client_secret_file"),
                path, f"[bucket.{name}].client_secret")
            refresh_token = _resolve_secret(
                section.get("refresh_token"), section.get("refresh_token_file"),
                path, f"[bucket.{name}].refresh_token")
        elif kind == "onedrive" and (not site or not site.strip()):
            raise SocialConfigError(
                f"{path}: [bucket.{name}].site (the SharePoint origin, e.g. "
                f"https://ubcca.sharepoint.com) is required for onedrive buckets")

        buckets.append(BucketConfig(
            name=str(name).strip(),
            kind=kind,
            root=root.strip() if root else "",
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
            source=source,
            site=site.strip() if site else None,
            mira_url=(mira or {}).get("url") if source == "mira" else None,
            mira_token=(mira or {}).get("token") if source == "mira" else None,
            mira_verify_tls=bool((mira or {}).get("verify_tls"))
            if source == "mira" else False,
        ))
    return buckets
