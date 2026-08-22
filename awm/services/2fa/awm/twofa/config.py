"""Config for the awm 2fa service.

Unlike the standalone ``virtual-auth`` daemon (which read a ``config.toml``),
this service is a gateway-managed process: its few tunables come from the
environment with sane defaults, and the device credentials live under the
workspace runtime dir ``$AWM_DIR/services/2fa/`` (the gitignored ``.awm/``).

The service holds **multiple Duo devices** at once, each fully independent. A
device is a ``(creds.json, key.pem)`` pair on disk, named by its filename:

  $AWM_DIR/services/2fa/creds.json          # the legacy/default device -> "cwl"
  $AWM_DIR/services/2fa/device_key.pem
  $AWM_DIR/services/2fa/alliance-creds.json # any "<name>-creds.json" -> "<name>"
  $AWM_DIR/services/2fa/alliance-key.pem

All credential files are mode ``0600`` and never committed. Provision them by
copying mira's existing devices into that dir or by enrolling a fresh device
via the ``2fa_activate`` verb (which writes ``paths_for(<name>)``).

Env overrides (all optional):

  AWM_2FA_DEDUP_SECONDS        engine dedup window           (default 3.0)
  AWM_2FA_APPROVE_ALL_MINUTES  approve-all window length     (default 5.0)
  AWM_2FA_HOLD_TTL_SECONDS     drop a held tx after          (default 120.0)
  AWM_2FA_BURST_WINDOW         burst poll window seconds     (default 60.0)
  AWM_2FA_BURST_INTERVAL       burst poll interval seconds   (default 1.0)
  AWM_2FA_SOCIAL_SUBSCRIBE     listen to the social service's slash commands
                               (default: on, unless AWM_TWOFA_PEER names an
                               owner — a borrowing node must not arm its own
                               burst off the owner's /approve)
  AWM_2FA_SOCIAL_WINDOW        social-armed burst window sec (default 300.0 = 5 min)
  AWM_2FA_SOCIAL_INTERVAL      social-armed burst poll sec   (default 2.0)
  AWM_2FA_SOCIAL_COUNT         social-armed expected count   (default 1000 = stay armed)
  AWM_2FA_SOCIAL_DEVICE        device when the command omits one (default cwl)
  AWM_2FA_DIR                  override the device-creds directory wholesale
  AWM_2FA_CREDS_PATH           force a single device's creds.json path
  AWM_2FA_KEY_PATH             force a single device's device_key.pem path
  AWM_2FA_DEVICE_NAME          name for the forced single device (default cwl)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from awm import config as _awm_config


def _service_dir() -> Path:
    """The device-creds directory.

    Defaults to ``$AWM_DIR/services/2fa`` (alongside the other services' DBs
    under ``.awm/``), but ``AWM_2FA_DIR`` overrides it wholesale — needed under
    ``awm dev shadow``, which rewrites ``AWM_WORKSPACE`` to a sandbox dir that
    doesn't hold the real device creds.
    """
    override = os.environ.get("AWM_2FA_DIR")
    if override and override.strip():
        return Path(override.strip())
    return _awm_config.AWM_DIR / "services" / "2fa"


# Per-service runtime dir (resolved at import; AWM_2FA_DIR-overridable).
SERVICE_DIR: Path = _service_dir()

# The legacy device (mira's original creds.json/device_key.pem) maps to this name.
DEFAULT_DEVICE_NAME = "cwl"
_LEGACY_CREDS = "creds.json"
_LEGACY_KEY = "device_key.pem"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _owns_the_singleton() -> bool:
    """True when this node is the canonical 2fa, i.e. borrows it from nobody.

    2fa is a fleet singleton because Duo's attempt budget is per *account*, not
    per host: if every node listens for ``/approve`` and arms its own burst, one
    slash command spends the budget N times over and N nodes reply into the same
    DM. So the listener belongs to the owner alone, and ownership is already
    stated by ``AWM_TWOFA_PEER`` — set on a borrower, unset on the owner. Reusing
    that selector rather than a second flag is what stops the two from
    disagreeing; a node cannot end up borrowing the service while still acting as
    its owner. ``AWM_2FA_SOCIAL_SUBSCRIBE`` still overrides, for the deliberate
    second listener.
    """
    from awm import gatewayclient

    return gatewayclient.peer_env("AWM_TWOFA_PEER") is None


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


@dataclass(frozen=True)
class DeviceCreds:
    """An on-disk Duo device: a name plus its two credential file paths."""

    name: str
    creds_path: Path
    key_path: Path

    @property
    def enrolled(self) -> bool:
        """True iff both credential files exist on disk."""
        return self.creds_path.exists() and self.key_path.exists()


def paths_for(name: str, service_dir: Path = SERVICE_DIR) -> DeviceCreds:
    """Canonical credential paths for a device *name*.

    The default device (:data:`DEFAULT_DEVICE_NAME`) keeps the legacy filenames
    so existing ``creds.json`` / ``device_key.pem`` continue to work; every
    other name uses ``"{name}-creds.json"`` / ``"{name}-key.pem"``.
    """
    if name == DEFAULT_DEVICE_NAME:
        return DeviceCreds(name, service_dir / _LEGACY_CREDS, service_dir / _LEGACY_KEY)
    return DeviceCreds(
        name, service_dir / f"{name}-creds.json", service_dir / f"{name}-key.pem")


def discover_devices(service_dir: Path = SERVICE_DIR) -> dict[str, DeviceCreds]:
    """Enumerate every on-disk device cred pair into a ``{name: DeviceCreds}`` map.

    Listing only — never loads or parses a device here, so one bad device can't
    block the others. A named creds file with no sibling key is still listed
    (``enrolled=False``); callers skip un-enrolled devices.

    Resolution order:

      1. If ``AWM_2FA_CREDS_PATH`` / ``AWM_2FA_KEY_PATH`` are set, return a
         single device named by ``AWM_2FA_DEVICE_NAME`` (default ``cwl``) —
         the back-compat / test escape hatch.
      2. Otherwise: map the legacy ``creds.json`` / ``device_key.pem`` pair to
         ``cwl``, then glob ``*-creds.json`` for every other device.
    """
    forced_creds = os.environ.get("AWM_2FA_CREDS_PATH")
    forced_key = os.environ.get("AWM_2FA_KEY_PATH")
    if forced_creds or forced_key:
        name = os.environ.get("AWM_2FA_DEVICE_NAME") or DEFAULT_DEVICE_NAME
        creds = Path(forced_creds) if forced_creds else (service_dir / _LEGACY_CREDS)
        key = Path(forced_key) if forced_key else (service_dir / _LEGACY_KEY)
        return {name: DeviceCreds(name, creds, key)}

    devices: dict[str, DeviceCreds] = {}
    # Legacy / default device first so it wins over any stray "cwl-creds.json".
    devices[DEFAULT_DEVICE_NAME] = paths_for(DEFAULT_DEVICE_NAME, service_dir)
    if service_dir.is_dir():
        # "*-creds.json" never matches the bare "creds.json" (the dash guards it).
        for creds_path in sorted(service_dir.glob("*-creds.json")):
            name = creds_path.name[: -len("-creds.json")]
            devices.setdefault(name, paths_for(name, service_dir))
    return devices


@dataclass(frozen=True)
class Config:
    devices: dict[str, DeviceCreds] = field(default_factory=discover_devices)

    # Engine tunables.
    dedup_seconds: float = field(default_factory=lambda: _env_float("AWM_2FA_DEDUP_SECONDS", 3.0))
    approve_all_minutes: float = field(
        default_factory=lambda: _env_float("AWM_2FA_APPROVE_ALL_MINUTES", 5.0))
    hold_ttl_seconds: float = field(
        default_factory=lambda: _env_float("AWM_2FA_HOLD_TTL_SECONDS", 120.0))

    # Burst-poll tunables.
    burst_window_seconds: float = field(
        default_factory=lambda: _env_float("AWM_2FA_BURST_WINDOW", 60.0))
    burst_interval_seconds: float = field(
        default_factory=lambda: _env_float("AWM_2FA_BURST_INTERVAL", 1.0))

    # Social slash-command subscription (Discord /approve → arm a burst).
    # Defaults to "only the node that OWNS 2fa listens" — see
    # :func:`_owns_the_singleton`.
    social_subscribe: bool = field(
        default_factory=lambda: _env_bool(
            "AWM_2FA_SOCIAL_SUBSCRIBE", _owns_the_singleton()))
    social_window_seconds: float = field(
        default_factory=lambda: _env_float("AWM_2FA_SOCIAL_WINDOW", 300.0))
    social_interval_seconds: float = field(
        default_factory=lambda: _env_float("AWM_2FA_SOCIAL_INTERVAL", 2.0))
    # Large default so the window stays armed for its full length (the burst
    # loop exits early once expected hits 0 — a big count keeps approving every
    # push until the deadline rather than stopping after the first).
    social_burst_count: int = field(
        default_factory=lambda: _env_int("AWM_2FA_SOCIAL_COUNT", 1000))
    social_default_device: str = field(
        default_factory=lambda: _env_str("AWM_2FA_SOCIAL_DEVICE", "cwl"))

    @property
    def enrolled_devices(self) -> dict[str, DeviceCreds]:
        """Subset of :attr:`devices` whose cred files both exist on disk."""
        return {n: d for n, d in self.devices.items() if d.enrolled}
