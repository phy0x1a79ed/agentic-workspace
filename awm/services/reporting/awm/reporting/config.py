"""The reporting service's switches and paths, all read from the environment at call time."""

from __future__ import annotations

import os
from pathlib import Path


def _float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name) or default)
    except ValueError:
        return default
    return value if value > 0 else default


def flag_on() -> bool:
    return (os.environ.get("AWM_FRONT_DOOR") or "").strip() == "1"


def task_refusal() -> str | None:
    """Why this process must not track tasks, or None if it may. Every fleet node may."""
    from awm.config import node_role

    try:
        role = node_role()
    except ValueError as exc:
        return f"the node role is invalid: {exc}"
    if role == "station":
        return "this node is a station; a station tracks no tasks"
    return None


def refusal() -> str | None:
    """Why this process must not run the front door, or None if it may."""
    why = task_refusal()
    if why is not None:
        return why
    if not flag_on():
        return "the front door is switched off (AWM_FRONT_DOOR is not 1)"
    return None


def role() -> str:
    from awm.config import node_role

    try:
        return node_role()
    except ValueError:
        return "invalid"


def swarm() -> str:
    from awm.config import node_swarm

    return node_swarm()


def db_path() -> Path:
    """The ledger's database: ``AWM_REPORTING_DB``, else the standard service path."""
    override = os.environ.get("AWM_REPORTING_DB")
    if override:
        return Path(override)
    from awm.persistence.databases import service_db_path

    return service_db_path("reporting")


def state_dir() -> Path:
    """Where the reconcile lock lives: ``AWM_REPORTING_STATE``, else beside the database."""
    override = os.environ.get("AWM_REPORTING_STATE")
    if override:
        return Path(override)
    return db_path().parent


def board_target() -> tuple[str, str] | str:
    """The board's URL and this swarm's token, or the reason there is none."""
    url = (os.environ.get("AWM_BOARD_URL") or "").strip()
    token = (os.environ.get("AWM_BOARD_TOKEN") or "").strip()
    if not url:
        return "AWM_BOARD_URL is not set on this node"
    if not token:
        return "AWM_BOARD_TOKEN is not set on this node"
    return url, token


def reconcile_interval_s() -> float:
    return _float("AWM_REPORTING_INTERVAL_S", 60.0)


def catchup_interval_s() -> float:
    """How often the board is re-read to close a gap the stream cannot replay."""
    return _float("AWM_REPORTING_CATCHUP_S", 300.0)


def message_backlog_s() -> float:
    """How old a card or thread message may be and still be news for a catch-up to tell Tony about.

    A card stays on the board for ever, so without a window the first
    catch-up would tell him about every card the swarm was ever sent.
    """
    return _float("AWM_REPORTING_MESSAGE_BACKLOG_S", 3 * 86400.0)


def quiet_swarms() -> frozenset[str]:
    """Swarms whose cards with this swarm never DM Tony, from ``AWM_REPORTING_QUIET_SWARMS``.

    A standing channel with another swarm is read by the session that holds it,
    so a direct message for each card on it is noise.
    """
    raw = os.environ.get("AWM_REPORTING_QUIET_SWARMS") or ""
    return frozenset(s.strip() for s in raw.replace(",", " ").split() if s.strip())


def new_agent_target() -> tuple[str, str]:
    """The project and scope `/new-agent` starts in."""
    return (os.environ.get("AWM_REPORTING_NEW_AGENT_PROJECT") or "awm",
            os.environ.get("AWM_REPORTING_NEW_AGENT_SCOPE") or "dev")


def projects_dir() -> Path:
    """The projects tree. Resolved as cx resolves it, so both see the same worktrees."""
    override = os.environ.get("AWM_CX_PROJECTS")
    if override:
        return Path(override)
    from awm import config as awm_config

    return awm_config.canonical_workspace() / "projects"


def project_scope(cwd: str | None) -> tuple[str | None, str | None]:
    """Project and scope from a directory under the projects tree, else None."""
    from awm import config as awm_config

    return awm_config.scope_of(cwd, projects_dir())


def remind_s() -> float:
    """How long a task stays blocked on an urgent request before Tony is reminded once."""
    return _float("AWM_REPORTING_REMIND_S", 3600.0)


def dm_target() -> tuple[str, str] | None:
    """The social account and channel that carry an urgent request to the operator, or None."""
    from awm.config import operator_dm

    target = operator_dm()
    if target is None:
        return None
    account, channel = target
    return os.environ.get("AWM_REPORTING_DM_ACCOUNT") or account, channel


# The tony swarm's Arbutus admins answer approvals in Slack #arbutus.
DEFAULT_APPROVAL_CHANNEL = "slack-bot:C0C9XNR77ML"
DEFAULT_APPROVAL_ADMINS = "U01Q4CDSTFX=tony,UC7RUF67P=ryan"


def approval_channel() -> tuple[str, str] | None:
    """The social account and channel that carry admin approval requests, or None.

    ``AWM_APPROVAL_CHANNEL`` is ``account:channel``. Unset, a node of the default
    swarm uses #arbutus and any other swarm takes no approvals.
    """
    from awm.config.peerbook import DEFAULT_SWARM

    raw = (os.environ.get("AWM_APPROVAL_CHANNEL") or "").strip()
    if not raw and swarm() == DEFAULT_SWARM:
        raw = DEFAULT_APPROVAL_CHANNEL
    account, _, channel = raw.partition(":")
    return (account, channel) if account and channel else None


def approval_admins() -> dict[str, str]:
    """Platform user id → admin name, from ``AWM_APPROVAL_ADMINS`` (``id=name,id=name``)."""
    from awm.config.peerbook import DEFAULT_SWARM

    raw = os.environ.get("AWM_APPROVAL_ADMINS")
    if raw is None:
        raw = DEFAULT_APPROVAL_ADMINS if swarm() == DEFAULT_SWARM else ""
    pairs = (p.strip().partition("=") for p in raw.replace(",", " ").split())
    return {uid: name for uid, _, name in pairs if uid and name}


def admin_approve_cmd() -> Path:
    return Path(os.environ.get("AWM_ADMIN_APPROVE") or Path.home() / ".ssh" / "awm-admin-approve")


def approval_bundles_dir() -> Path:
    """Where agents write bundles: the same ``ARB_BUNDLES`` the approve script reads."""
    return Path(os.environ.get("ARB_BUNDLES") or Path.home() / ".local" / "share" / "arbutus-approvals")


def approval_pending_dir() -> Path:
    """The frozen copies. Under ~/.ssh/admin-approvals, where the admin-gate hook keeps Claude sessions out."""
    return Path(os.environ.get("AWM_APPROVAL_PENDING")
                or Path.home() / ".ssh" / "admin-approvals" / "pending")


def trilium_url() -> str | None:
    """Where Tony opens a Trilium note from, up to the ``#``, or None when no edge is known."""
    override = (os.environ.get("AWM_REPORTING_TRILIUM_URL") or "").strip()
    if override:
        return override.rstrip("/")
    from awm.config import edge_url

    edge = edge_url()
    return f"{edge}/trilium" if edge else None
