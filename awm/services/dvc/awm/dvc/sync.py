"""Append-only sync of the shared DVC cache to chinook — the daily backup.

Chinook is the **remote for the cache the way GitHub is the remote for the
code**: it accumulates objects and never prunes. That single property is what
makes a local ``dvc gc`` survivable — collection on a cache shared by every
project in the workspace is exactly the operation most likely to be wrong, and
with delete-propagation retired a mistake stays recoverable instead of becoming
permanent at the next tick.

WHY THIS IS ONE TRANSFER ITEM
Its counterpart :mod:`awm.dvc.backup` has to partition a 100k-file tree by hand
to carve out the DVC checkouts — hardlinks into this cache, which Globus cannot
preserve, so mirroring both would upload the same bytes twice. This job has no
such problem: it is the cache, whole, as a single recursive item.

The path this writes to is the path :mod:`awm.dvc.transfer` reads back from:
``<prefix>/data/.dvc_cache/files/md5/<shard>/<rest>``. They must not drift. It
is also a sibling of, never a parent of, the mirror's ``<prefix>/workspace/`` —
which is what stops the one transfer that deletes from reaching this one.

This module submits and nothing else. Deciding whether a submission is allowed,
and recording that it happened, belong to :mod:`awm.dvc.jobs` — the guard used
to be a ``threading.Lock`` here, which could only ever cover one of the three
processes that submit.
"""

from __future__ import annotations

import logging
from typing import Any

from awm.dvc import globus
from awm.dvc.config import SHARED_CACHE, WORKSPACE_ROOT, ChinookConfig

log = logging.getLogger("awm.dvc.sync")

SYNC_LABEL = "agentic_workspace dvc cache daily"


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


def submit(cfg: ChinookConfig, items: list[dict]) -> str:
    """Submit the cache document. The append-only flags live here, in one place."""
    return globus.submit(
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


def sync(*, dry_run: bool = False) -> dict[str, Any]:
    """Build the cache document and submit it, returning the task id.

    Single-flight is *not* enforced here: it is a partial unique index in the
    run table (:mod:`awm.dvc.runs`). Callers go through :mod:`awm.dvc.jobs`.
    """
    from awm.dvc.config import load

    cfg = load()

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

    return {**result, "submitted": True, "task_id": submit(cfg, items)}
