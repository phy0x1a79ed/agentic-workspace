"""Config for the awm 2fa service.

Unlike the standalone ``virtual-auth`` daemon (which read a ``config.toml``),
this service is a gateway-managed process: its few tunables come from the
environment with sane defaults, and the device credentials live under the
workspace runtime dir ``$AWM_DIR/services/2fa/`` (the gitignored ``.awm/``).

Credential files (both mode ``0600``, never committed):

  $AWM_DIR/services/2fa/creds.json     # Duo akey/pkey + host
  $AWM_DIR/services/2fa/device_key.pem # device RSA private key

Provision them either by copying mira's existing device
(``scp mira:~/.config/virtual-auth/{creds.json,device_key.pem}`` into that dir)
or by enrolling a fresh device via the ``2fa_activate`` verb.

Env overrides (all optional):

  AWM_2FA_DEDUP_SECONDS        engine dedup window           (default 3.0)
  AWM_2FA_APPROVE_ALL_MINUTES  approve-all window length     (default 5.0)
  AWM_2FA_BURST_THRESHOLD      >N pending in a fetch = burst (default 1)
  AWM_2FA_HOLD_TTL_SECONDS     drop a held tx after          (default 120.0)
  AWM_2FA_BURST_WINDOW         burst poll window seconds     (default 60.0)
  AWM_2FA_BURST_INTERVAL       burst poll interval seconds   (default 1.0)
  AWM_2FA_BURST_EXIT_ON_APPROVE  stop on first approval      (default 1/true)
  AWM_2FA_CREDS_PATH           override creds.json path
  AWM_2FA_KEY_PATH             override device_key.pem path
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from awm import config as _awm_config

# Per-service runtime dir, alongside the other services' DBs under .awm/.
SERVICE_DIR: Path = _awm_config.AWM_DIR / "services" / "2fa"


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
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _default_creds_path() -> Path:
    return Path(os.environ.get("AWM_2FA_CREDS_PATH") or (SERVICE_DIR / "creds.json"))


def _default_key_path() -> Path:
    return Path(os.environ.get("AWM_2FA_KEY_PATH") or (SERVICE_DIR / "device_key.pem"))


@dataclass(frozen=True)
class Config:
    creds_path: Path = field(default_factory=_default_creds_path)
    key_path: Path = field(default_factory=_default_key_path)

    # Engine tunables.
    dedup_seconds: float = field(default_factory=lambda: _env_float("AWM_2FA_DEDUP_SECONDS", 3.0))
    approve_all_minutes: float = field(
        default_factory=lambda: _env_float("AWM_2FA_APPROVE_ALL_MINUTES", 5.0))
    burst_threshold: int = field(default_factory=lambda: _env_int("AWM_2FA_BURST_THRESHOLD", 1))
    hold_ttl_seconds: float = field(
        default_factory=lambda: _env_float("AWM_2FA_HOLD_TTL_SECONDS", 120.0))

    # Burst-poll tunables.
    burst_window_seconds: float = field(
        default_factory=lambda: _env_float("AWM_2FA_BURST_WINDOW", 60.0))
    burst_interval_seconds: float = field(
        default_factory=lambda: _env_float("AWM_2FA_BURST_INTERVAL", 1.0))
    burst_exit_on_approve: bool = field(
        default_factory=lambda: _env_bool("AWM_2FA_BURST_EXIT_ON_APPROVE", True))

    @property
    def enrolled(self) -> bool:
        """True iff both credential files exist on disk."""
        return self.creds_path.exists() and self.key_path.exists()
