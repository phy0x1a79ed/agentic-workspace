"""VPN profile config.

Lives at ``$AWM_DIR/vpn.toml`` (workspace-local; chmod 0600 — it holds VPN
credentials). One ``[<profile>]`` table per VPN exit. Shape::

    [ubc]
    # image defaults to "awm-vpn-ubc"; build it from containers/ubc/.
    server        = "myvpn.ubc.ca"
    user          = "cwl@app"
    password      = "..."
    second_factor = "push"          # optional; line fed to openconnect's 2FA prompt
    # UBC's Duo push is auto-approved by the local awm `2fa` service: vpn_up arms
    # a burst on this device right before dialing. Defaults to "cwl"; "" disables.
    twofa_device  = "cwl"

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

import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from awm import config

CONFIG_FILE: Path = config.AWM_DIR / "vpn.toml"

# The profiles this service knows how to dial. Each maps to a container image.
PROFILES: tuple[str, ...] = ("ubc", "proton")
DEFAULT_IMAGE = {"ubc": "awm-vpn-ubc", "proton": "awm-vpn-proton"}

# Each profile's co-located bounce sshd is published on the host at this port, so
# `ssh -W %h:%p` / ProxyJump through localhost:<port> egresses via that tunnel
# (the vpn_bounce architecture). ubc=2222 matches the legacy vpn_bounce port, so
# ~/.ssh/config's `vpn_ubc` Port line is unchanged across the cutover.
BOUNCE_PORT = {"ubc": 2222, "proton": 2223}

# The vpn service's own dir (alongside vpn.db) — holds the bounce keypair.
SERVICE_DIR: Path = config.AWM_DIR / "services" / "vpn"


def bounce_port(profile: str) -> int:
    """Host port the profile's bounce sshd is published on (KeyError if unknown)."""
    return BOUNCE_PORT[profile]


def bounce_key_path() -> Path:
    """Host path of the bounce SSH private key (0600); public key is `.pub`."""
    return SERVICE_DIR / "bounce"


def ensure_bounce_key() -> str:
    """Generate the bounce keypair on first use; return the public key text.

    One keypair serves every profile: the public key is injected into each
    container as the sole authorized key (see ``ssh_server``), and the private
    key is what ``~/.ssh/config``'s bounce host points ``IdentityFile`` at.
    ed25519, no passphrase, stored 0600 under :data:`SERVICE_DIR`.
    """
    priv = bounce_key_path()
    pub = priv.with_suffix(".pub")
    if not pub.exists():
        SERVICE_DIR.mkdir(parents=True, exist_ok=True)
        # Remove a half-written private key so ssh-keygen doesn't prompt to overwrite.
        priv.unlink(missing_ok=True)
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "awm-vpn-bounce",
             "-f", str(priv)],
            check=True, capture_output=True, text=True,
        )
        priv.chmod(0o600)
    return pub.read_text().strip()


class VpnConfigError(Exception):
    """Raised when vpn.toml is missing or a profile section is malformed."""


@dataclass(frozen=True)
class ProfileConfig:
    profile: str
    image: str
    # env injected into the container at `docker run -e KEY=VAL`.
    env: dict[str, str] = field(default_factory=dict)
    # 2fa device whose Duo burst the host arms before dialing (None = no 2FA).
    twofa_device: str | None = None


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

    # The bounce sshd in every profile authorizes this one public key.
    bounce_pubkey = ensure_bounce_key()
    twofa_device: str | None = None

    if profile == "ubc":
        # UBC's Duo push is auto-approved by the local awm `2fa` service: the
        # host-side `vpn_up` arms a burst on this device right before dialing
        # (see container.py). Key absent => default "cwl"; present-but-blank =>
        # disabled (None); otherwise the named device.
        raw_device = section.get("twofa_device")
        if raw_device is None:
            twofa_device = "cwl"
        elif isinstance(raw_device, str) and raw_device.strip():
            twofa_device = raw_device.strip()
        else:
            twofa_device = None
        env = {
            "UBC_SERVER": _req(section, "server", profile),
            "UBC_USER": _req(section, "user", profile),
            "UBC_PASSWORD": _req(section, "password", profile),
            "UBC_SECOND_FACTOR": _opt(section, "second_factor", "push"),
            "BOUNCE_AUTHORIZED_KEY": bounce_pubkey,
        }
    elif profile == "proton":
        env = {
            "PROTON_USERNAME": _req(section, "username", profile),
            "PROTON_PASSWORD": _req(section, "password", profile),
            "PROTON_SERVER": _opt(section, "server"),
            "BOUNCE_AUTHORIZED_KEY": bounce_pubkey,
        }
    else:  # pragma: no cover - guarded by the PROFILES check above
        env = {}

    return ProfileConfig(profile=profile, image=image, env=env,
                         twofa_device=twofa_device)
