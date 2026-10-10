"""The door's switches and paths, all read from the environment at call time."""

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


def refusal() -> str | None:
    """Why this process must not run the door, or None if it may.

    A station is refused first, so its status says so whatever the flag holds.
    """
    from awm.config import node_role

    try:
        role = node_role()
    except ValueError as exc:
        return f"the node role is invalid: {exc}"
    if role == "station":
        return "this node is a station; a station cannot run a front door"
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
    """The queue's database: ``AWM_DOOR_DB``, else the standard service path."""
    override = os.environ.get("AWM_DOOR_DB")
    if override:
        return Path(override)
    from awm.persistence.databases import service_db_path

    return service_db_path("door")


def state_dir() -> Path:
    """Where the reconcile lock lives: ``AWM_DOOR_STATE``, else beside the queue."""
    override = os.environ.get("AWM_DOOR_STATE")
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
    return _float("AWM_DOOR_INTERVAL_S", 60.0)


def catchup_interval_s() -> float:
    """How often the board is re-read to close a gap the stream cannot replay."""
    return _float("AWM_DOOR_CATCHUP_S", 300.0)


def notify_batch_s() -> float:
    """How long a new card waits for others before the representative is told."""
    return _float("AWM_DOOR_NOTIFY_BATCH_S", 5.0)


def notify_retry_s() -> float:
    """How long to wait before telling the representative again after a failure."""
    return _float("AWM_DOOR_NOTIFY_RETRY_S", 30.0)


def message_backlog_s() -> float:
    """How old a posted message card may be and still be queued by a catch-up.

    A message card stays ``posted`` for ever, so without a window the first
    catch-up would queue every message the swarm was ever sent.
    """
    return _float("AWM_DOOR_MESSAGE_BACKLOG_S", 3 * 86400.0)


def reannounce_s() -> float:
    """How long a card may sit queued before the representative is told about it again.

    ``AWM_DOOR_REANNOUNCE_MIN`` is in minutes; the default is 10.
    """
    return _float("AWM_DOOR_REANNOUNCE_MIN", 10.0) * 60.0
