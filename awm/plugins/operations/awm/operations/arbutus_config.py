"""Where the Arbutus approval path finds its Slack thread, its admins, its bundles and its script."""

from __future__ import annotations

import os
from pathlib import Path

# The Arbutus admins answer approvals in Slack #arbutus.
DEFAULT_APPROVAL_CHANNEL = "slack-bot:C0C9XNR77ML"
DEFAULT_APPROVAL_ADMINS = "U01Q4CDSTFX=tony,UC7RUF67P=ryan"


def approval_channel() -> tuple[str, str] | None:
    """``AWM_APPROVAL_CHANNEL`` is ``account:channel``. Unset, #arbutus; set empty, none."""
    raw = os.environ.get("AWM_APPROVAL_CHANNEL")
    raw = DEFAULT_APPROVAL_CHANNEL if raw is None else raw.strip()
    account, _, channel = raw.partition(":")
    return (account, channel) if account and channel else None


def approval_admins() -> dict[str, str]:
    """Platform user id → admin name, from ``AWM_APPROVAL_ADMINS`` (``id=name,id=name``)."""
    raw = os.environ.get("AWM_APPROVAL_ADMINS")
    if raw is None:
        raw = DEFAULT_APPROVAL_ADMINS
    pairs = (p.strip().partition("=") for p in raw.replace(",", " ").split())
    return {uid: name for uid, _, name in pairs if uid and name}


def admin_approve_cmd() -> Path:
    return Path(os.environ.get("AWM_ADMIN_APPROVE") or Path.home() / ".ssh" / "awm-admin-approve")


def approval_bundles_dir() -> Path:
    """Where agents write bundles: the same ``ARB_BUNDLES`` the approve script reads."""
    return Path(os.environ.get("ARB_BUNDLES") or Path.home() / ".local" / "share" / "arbutus-approvals")


def approval_pending_dir() -> Path:
    """The frozen copies, under ~/.ssh/admin-approvals, where the admin-gate hook keeps sessions out."""
    return Path(os.environ.get("AWM_APPROVAL_PENDING")
                or Path.home() / ".ssh" / "admin-approvals" / "pending")
