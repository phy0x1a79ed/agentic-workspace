"""Append-only sync of the shared DVC cache to chinook — the daily backup.

Chinook is the **remote for the cache the way GitHub is the remote for the
code**: it accumulates objects and never prunes. That single property is what
makes a local ``dvc gc`` survivable — collection on a cache shared by every
project in the workspace is exactly the operation most likely to be wrong, and
with delete-propagation retired a mistake stays recoverable instead of becoming
permanent at the next tick.

WHY THIS IS ONE TRANSFER ITEM
The predecessor mirrored the whole workspace and had to partition a 100k-file
tree by hand to carve out the DVC checkouts (hardlinks into the cache, which
Globus cannot preserve, so mirroring both uploaded the same bytes twice). Sync
only the cache and that entire problem — and the fragile pairing it rested on,
"excluding the checkouts is only safe because the cache is not excluded" —
stops existing. Everything the cache does not cover is covered by GitHub or is
deliberately disposable; :mod:`awm.dvc.coverage` is what makes that visible.

The path this writes to is the path :mod:`awm.dvc.transfer` reads back from:
``<prefix>/data/.dvc_cache/files/md5/<shard>/<rest>``. They must not drift.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from awm import config as awm_config
from awm.dvc import globus
from awm.dvc.config import SHARED_CACHE, WORKSPACE_ROOT, ChinookConfig

log = logging.getLogger("awm.dvc.sync")

SYNC_LABEL = "agentic_workspace dvc cache daily"

# Where the last submitted sync task id is remembered, so a run can decline to
# stack a second full-cache scan on one still in flight.
_STATE_FILE: Path = awm_config.SERVICES_DIR / "dvc" / "last_sync.json"

_SUBMIT_LOCK = threading.Lock()


def _read_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(task_id: str) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps({"task_id": task_id}))


def _in_flight(cfg: ChinookConfig) -> str | None:
    """Task id of a still-running previous sync, if there is one."""
    prev = _read_state().get("task_id")
    if not prev:
        return None
    try:
        return prev if not globus.task_status(cfg, prev)["done"] else None
    except globus.GlobusError:
        # An unqueryable task id is not a reason to block the backup.
        return None


def build_items(cfg: ChinookConfig) -> list[dict]:
    """The transfer document: the shared cache, recursively, and nothing else."""
    rel = SHARED_CACHE.relative_to(WORKSPACE_ROOT)
    return [
        {
            "DATA_TYPE": "transfer_item",
            "source_path": f"{SHARED_CACHE}/",
            "destination_path": f"{cfg.prefix}/{rel}/",
            "recursive": True,
        }
    ]


def sync(*, dry_run: bool = False, force: bool = False) -> dict[str, Any]:
    """Submit the cache sync and return its task id."""
    from awm.dvc.config import load

    cfg = load()

    with _SUBMIT_LOCK:
        if not dry_run and not force:
            if prev := _in_flight(cfg):
                return {
                    "submitted": False,
                    "in_flight": prev,
                    "note": (
                        f"sync task {prev} is still running — a second full-cache "
                        "scan would only duplicate its work. Pass force=true to "
                        "submit anyway."
                    ),
                }

        if not SHARED_CACHE.is_dir():
            raise FileNotFoundError(
                f"shared cache {SHARED_CACHE} does not exist — refusing to sync "
                "nothing, which on an append-only remote is a silent no-op rather "
                "than an error you would notice"
            )

        items = build_items(cfg)
        result: dict[str, Any] = {
            "source": str(SHARED_CACHE),
            "destination": f"{cfg.remote_endpoint}:{items[0]['destination_path']}",
            "append_only": True,
        }
        if dry_run:
            return {**result, "dry_run": True, "transfer_items": items}

        task_id = globus.submit(
            cfg,
            items,
            source=cfg.local_endpoint,
            destination=cfg.remote_endpoint,
            label=SYNC_LABEL,
            # 1 = copy when size differs. Cache objects are immutable and
            # content-addressed, so a same-size file IS the right file; this
            # still re-copies whatever a previously interrupted task truncated.
            sync_level=1,
            # THE property that makes chinook a remote rather than a mirror. A
            # local `dvc gc` must never be able to reach across the wire, so
            # this is asserted in tests, not merely written here.
            delete_destination_extra=False,
            # A scan of a 50k-object store will always race a concurrent `dvc
            # add` or `gc`; one vanished object must not fail the backup.
            skip_source_errors=True,
        )
        _write_state(task_id)
        return {**result, "submitted": True, "task_id": task_id}
