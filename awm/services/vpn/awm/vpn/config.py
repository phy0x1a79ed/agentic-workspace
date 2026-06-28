"""VPN profile config.

Lives at ``$AWM_DIR/vpn.toml`` (workspace-local; chmod 0600 — it holds VPN
credentials and the virtual-auth token). One ``[<profile>]`` table per VPN exit.
Shape::

    [ubc]
    # image defaults to "awm-vpn-ubc"; build it from containers/ubc/.
    server        = "myvpn.ubc.ca"
    user          = "cwl@app"
    password      = "..."
    second_factor = "push"          # optional; line fed to openconnect's 2FA prompt
    # virtual-auth on-demand Duo auto-approver (mira). The ubc dialer POSTs a
    # "burst" here right before dialing so the Duo push auto-approves (~1s).
    va_burst_url  = "http://10.74.81.111:8077/burst"
    va_token      = "..."           # the [serve] token from mira's virtual-auth config

    [proton]
    # image defaults to "awm-vpn-proton"; build it from containers/proton/.
    username = "..."
    password = "..."
    server   = ""                   # optional protonvpn-cli connect target (e.g. country code); empty = fastest

The config is read lazily — only when an ``up`` for that profile actually runs —
so a missing file is fine until you try to bring an exit up, at which point we
fail loudly rather than dialing with blank creds.

``ProfileConfig.env`` is exactly the set of env vars injected into the profile's
container at ``docker run``; the dialer scripts inside the image read them.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from awm import config

CONFIG_FILE: Path = config.AWM_DIR / "vpn.toml"

# The profiles this service knows how to dial. Each maps to a container image.
PROFILES: tuple[str, ...] = ("ubc", "proton")
DEFAULT_IMAGE = {"ubc": "awm-vpn-ubc", "proton": "awm-vpn-proton"}


class VpnConfigError(Exception):
    """Raised when vpn.toml is missing or a profile section is malformed."""


@dataclass(frozen=True)
class ProfileConfig:
    profile: str
    image: str
    # env injected into the container at `docker run -e KEY=VAL`.
    env: dict[str, str] = field(default_factory=dict)


def _load_toml() -> dict:
    if not CONFIG_FILE.exists():
        raise VpnConfigError(
            f"{CONFIG_FILE} not found — create it with a [<profile>] table of "
            f"VPN credentials before bringing an exit up (see INSTALL.md)."
        )
    try:
        with open(CONFIG_FILE, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise VpnConfigError(f"could not parse {CONFIG_FILE}: {exc}") from exc


def _req(section: dict, key: str, profile: str) -> str:
    val = section.get(key)
    if not isinstance(val, str) or not val.strip():
        raise VpnConfigError(
            f"{CONFIG_FILE}: [{profile}].{key} must be a non-empty string"
        )
    return val.strip()


def _opt(section: dict, key: str, default: str = "") -> str:
    val = section.get(key)
    return val.strip() if isinstance(val, str) and val.strip() else default


def load_profile(profile: str) -> ProfileConfig:
    """Load + validate one profile's config, returning its container env.

    Raises :class:`VpnConfigError` if vpn.toml is missing, the section is
    absent, or a required field is blank.
    """
    if profile not in PROFILES:
        raise VpnConfigError(
            f"unknown profile {profile!r}; known profiles: {', '.join(PROFILES)}"
        )
    data = _load_toml()
    section = data.get(profile)
    if not isinstance(section, dict):
        raise VpnConfigError(f"{CONFIG_FILE} missing [{profile}] section")

    image = _opt(section, "image", DEFAULT_IMAGE[profile])

    if profile == "ubc":
        env = {
            "UBC_SERVER": _req(section, "server", profile),
            "UBC_USER": _req(section, "user", profile),
            "UBC_PASSWORD": _req(section, "password", profile),
            "UBC_SECOND_FACTOR": _opt(section, "second_factor", "push"),
            # virtual-auth burst: optional but strongly recommended for UBC's
            # Duo 2FA. If absent the dialer simply skips the burst (and the dial
            # will hang on Duo unless 2FA is otherwise satisfied).
            "VA_BURST_URL": _opt(section, "va_burst_url"),
            "VA_TOKEN": _opt(section, "va_token"),
        }
    elif profile == "proton":
        env = {
            "PROTON_USERNAME": _req(section, "username", profile),
            "PROTON_PASSWORD": _req(section, "password", profile),
            "PROTON_SERVER": _opt(section, "server"),
        }
    else:  # pragma: no cover - guarded by the PROFILES check above
        env = {}

    return ProfileConfig(profile=profile, image=image, env=env)
