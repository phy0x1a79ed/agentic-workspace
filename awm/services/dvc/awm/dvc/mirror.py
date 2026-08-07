"""Full-workspace mirror to chinook — the daily off-site backup.

This is **disaster recovery, not an archive**: it runs with
``delete_destination_extra``, so a local deletion propagates to chinook on the
next run. Nothing here protects you from ``rm``; it protects you from losing the
machine.

WHY THE TREE IS PARTITIONED BY HAND
Every output tracked by a ``*.dvc`` file is a hardlink into ``data/.dvc_cache``
and is fully reconstructible from it via ``dvc checkout``, so mirroring both the
cache and its checkouts uploads the same bytes many times over — Globus cannot
preserve hardlinks, which is what inflates a ~188 GB physical workspace to ~665 GB
on the wire. Excluding the checkouts is only *safe* because ``data/.dvc_cache``
itself is not excluded; keep that pairing intact or the exclusions stop being
recoverable and become data loss.

Globus ``filter_rules`` cannot express those exclusions: they match an item's
*name* at any depth, and DVC output names (``assembly``, ``hosts``, ``genomes``)
collide with unrelated real content elsewhere in the tree — a name-based rule
would silently drop e.g. ``projects/metasmith-libraries/*/transforms/assembly``
(actual source code) along with the checkout that shares its name. So
:func:`partition` walks the tree instead and emits, for each directory, either the
whole directory (nothing excluded beneath it), nothing (it *is* excluded), or a
recursion into it (something excluded lies below).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

import yaml

from awm import config as awm_config
from awm.dvc import globus
from awm.dvc.config import WORKSPACE_ROOT, ChinookConfig

log = logging.getLogger("awm.dvc.mirror")

MIRROR_LABEL = "agentic_workspace daily"

# Where the last submitted mirror task id is remembered, so a run can refuse to
# stack a second destructive mirror on top of one still in flight.
_STATE_FILE: Path = awm_config.SERVICES_DIR / "dvc" / "last_mirror.json"

_SUBMIT_LOCK = threading.Lock()


def dvc_output_paths(root: Path) -> list[str]:
    """Absolute paths of every DVC-tracked output under ``root``.

    Each ``*.dvc`` *file* (not the ``.dvc/`` config directories that share the
    extension) names its outputs by a ``path`` relative to the pin's own
    directory. All outs are collected, not just the first — a multi-output pin
    would otherwise leave its later checkouts to be mirrored redundantly.
    """
    out: set[str] = set()
    for pin in root.rglob("*.dvc"):
        if not pin.is_file():
            continue
        try:
            doc = yaml.safe_load(pin.read_text()) or {}
        except (OSError, yaml.YAMLError) as exc:
            log.warning("skipping unparseable pin %s: %s", pin, exc)
            continue
        for o in doc.get("outs") or []:
            if isinstance(o, dict) and o.get("path"):
                out.add(os.path.normpath(str(pin.parent / str(o["path"]))))
    return sorted(out)


def partition(root: str, excluded: list[str], dest_prefix: str) -> list[dict]:
    """Minimal set of transfer items covering ``root`` except ``excluded``."""
    root = os.path.normpath(root)
    excluded_set = {os.path.normpath(p) for p in excluded}

    # Directories with an excluded path somewhere beneath them must be recursed
    # into rather than emitted whole.
    ancestors: set[str] = {root}
    for p in excluded_set:
        d = os.path.dirname(p)
        while d and d != root and d not in ancestors:
            ancestors.add(d)
            d = os.path.dirname(d)

    items: list[dict] = []

    def walk(dir_path: str) -> None:
        try:
            entries = sorted(os.scandir(dir_path), key=lambda e: e.name)
        except (FileNotFoundError, PermissionError):
            return
        for entry in entries:
            full = os.path.normpath(entry.path)
            if full in excluded_set:
                continue
            dest = os.path.join(dest_prefix, os.path.relpath(full, root))
            if entry.is_dir(follow_symlinks=False):
                if full in ancestors:
                    walk(full)
                else:
                    items.append(
                        {
                            "DATA_TYPE": "transfer_item",
                            "source_path": full + "/",
                            "destination_path": dest + "/",
                            "recursive": True,
                        }
                    )
            else:
                items.append(
                    {
                        "DATA_TYPE": "transfer_item",
                        "source_path": full,
                        "destination_path": dest,
                        "recursive": False,
                    }
                )

    walk(root)
    return items


def _read_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(task_id: str) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps({"task_id": task_id}))


def _in_flight(cfg: ChinookConfig) -> str | None:
    """Task id of a still-running previous mirror, if there is one."""
    prev = _read_state().get("task_id")
    if not prev:
        return None
    try:
        return prev if not globus.task_status(cfg, prev)["done"] else None
    except globus.GlobusError:
        # An unqueryable task id is not a reason to block the backup.
        return None


def build_items(cfg: ChinookConfig) -> tuple[list[dict], int]:
    """Transfer items for a full mirror, plus the count of paths excluded."""
    excluded = dvc_output_paths(WORKSPACE_ROOT)
    items = partition(str(WORKSPACE_ROOT), excluded, cfg.prefix)
    return items, len(excluded)


def mirror(*, dry_run: bool = False, force: bool = False) -> dict[str, Any]:
    """Submit the full-workspace mirror and return its task id."""
    from awm.dvc.config import load

    cfg = load()

    with _SUBMIT_LOCK:
        if not dry_run and not force:
            if prev := _in_flight(cfg):
                return {
                    "submitted": False,
                    "in_flight": prev,
                    "note": (
                        f"mirror task {prev} is still running — a second destructive "
                        "mirror would race it. Pass force=true to submit anyway."
                    ),
                }

        items, excluded = build_items(cfg)
        result: dict[str, Any] = {
            "items": len(items),
            "excluded_dvc_outputs": excluded,
            "destination": f"{cfg.remote_endpoint}:{cfg.prefix}",
        }
        if dry_run:
            return {**result, "dry_run": True, "transfer_items": items}

        task_id = globus.submit(
            cfg,
            items,
            source=cfg.local_endpoint,
            destination=cfg.remote_endpoint,
            label=MIRROR_LABEL,
            # 2 = compare mtime+size. Unlike the content-addressed cache objects,
            # ordinary workspace files are mutated in place, so size alone misses
            # a same-length edit.
            sync_level=2,
            # The whole point: the remote is a mirror, not an accumulator.
            delete_destination_extra=True,
            # A 100k-file scan will always race some file being rewritten or
            # removed underneath it; one vanished file must not fail the backup.
            skip_source_errors=True,
        )
        _write_state(task_id)
        return {**result, "submitted": True, "task_id": task_id}
