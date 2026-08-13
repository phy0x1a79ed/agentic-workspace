"""Full-workspace mirror to chinook — the nightly backup of everything else.

This is the *backup* half of a deliberately split pair. :mod:`awm.dvc.sync`
pushes the shared DVC cache and never deletes, so it is an **archive** and a
local ``dvc gc`` stays recoverable. This module is the ordinary sense of the
word: it runs with ``delete_destination_extra``, so a local deletion propagates
to chinook on the next run. Nothing here protects you from ``rm``; it protects
you from losing the machine.

THE ONE COUPLING BETWEEN THE TWO JOBS
Every output tracked by a ``*.dvc`` file is a hardlink into ``data/.dvc_cache``
and is fully reconstructible from it via ``dvc checkout``. Globus cannot preserve
hardlinks, so mirroring both the cache and its checkouts uploads the same bytes
many times over. This job therefore excludes the checkouts — and that exclusion
is only *safe* because the cache itself reaches chinook by the other path. **The
mirror is only safe to run while the cache sync is also running.** Break that
pairing and the exclusions stop being recoverable and become data loss. The
scheduler is what keeps them paired; :func:`awm.dvc.coverage.report` is what
makes a break visible.

The cache is excluded here rather than shared between the jobs because the two
have opposite delete semantics, and this transfer is the destructive one. That
is also why it lands under ``<prefix>/workspace/`` — a sibling of the archive's
``<prefix>/data/.dvc_cache/`` rather than its parent. Every destination path this
module emits is under ``workspace/``, so a delete-enabled transfer *cannot* reach
the archive no matter what the exclusion logic gets wrong. Structure, not a rule.

WHY THE TREE IS PARTITIONED BY HAND
Globus ``filter_rules`` cannot express these exclusions: they match an item's
*name* at any depth, and DVC output names (``assembly``, ``hosts``, ``genomes``)
collide with unrelated real content elsewhere in the tree — a name-based rule
would silently drop e.g. ``projects/metasmith-libraries/*/transforms/assembly``
(actual source code) along with the checkout that shares its name. So
:func:`partition` walks the tree instead and emits, for each directory, either
the whole directory (nothing excluded beneath it), nothing (it *is* excluded), or
a recursion into it (something excluded lies below). Name matching still happens
for the regenerable directories, but in :func:`regenerable_paths`, where the walk
can see a full path and hands :func:`partition` exact absolute paths.

KNOWN LIMITATION
A top-level entry deleted locally is named by no transfer item at all, so it is
never deleted remotely. Path-partitioned mirroring can leave orphans at the root
of an excluded subtree; they cost storage, not correctness.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from awm.dvc import globus
from awm.dvc.config import SHARED_CACHE, WORKSPACE_ROOT, ChinookConfig

log = logging.getLogger("awm.dvc.backup")

BACKUP_LABEL = "agentic_workspace daily"

# The mirror's own root on the collection, a sibling of the cache archive's
# `<prefix>/data/.dvc_cache/`. See the module docstring — this is what makes the
# separation structural rather than a convention.
DEST_SUBDIR = "workspace"

# Directory names whose contents are rebuilt from what is already backed up:
# thousands of tiny files whose per-file transfer overhead dwarfs their value.
# Resolved to exact absolute paths by walking, never handed to Globus as names.
# `dist/` is deliberately absent — those are built bundles that a live server
# serves, and rebuilding one needs a toolchain that may not survive the machine.
REGENERABLE_DIRS = frozenset(
    {"__pycache__", "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)


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


def regenerable_paths(
    root: Path, *, names: frozenset[str] = REGENERABLE_DIRS, skip: list[str] | None = None
) -> list[str]:
    """Absolute paths of every directory under ``root`` named in ``names``.

    Prunes at each match rather than descending, and prunes at everything in
    ``skip`` — pass the cache and the DVC outputs, or this walks 300 GB of
    content-addressed objects to find nothing.
    """
    skip_set = {os.path.normpath(p) for p in (skip or [])}
    found: list[str] = []

    def walk(dir_path: str) -> None:
        try:
            entries = os.scandir(dir_path)
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            return
        with entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                full = os.path.normpath(entry.path)
                if full in skip_set:
                    continue
                if entry.name in names:
                    found.append(full)
                    continue
                walk(full)

    walk(os.path.normpath(str(root)))
    return sorted(found)


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


def destination_root(cfg: ChinookConfig) -> str:
    """The mirror's root on the collection — never the archive's parent."""
    return f"{cfg.prefix}/{DEST_SUBDIR}"


def build_items(cfg: ChinookConfig) -> tuple[list[dict], dict[str, int]]:
    """Transfer items for a full mirror, plus a breakdown of what was excluded.

    The pin scan is an ``rglob`` over the whole workspace and takes seconds to
    minutes; it is CPU- and IO-bound, so callers on an event loop must run this
    in a thread.
    """
    outs = dvc_output_paths(WORKSPACE_ROOT)
    cache = os.path.normpath(str(SHARED_CACHE))
    regen = regenerable_paths(WORKSPACE_ROOT, skip=[*outs, cache])
    excluded = [*outs, cache, *regen]
    items = partition(str(WORKSPACE_ROOT), excluded, destination_root(cfg))
    counts = {
        "dvc_outputs": len(outs),
        "shared_cache": 1,
        "regenerable_dirs": len(regen),
        "total": len(excluded),
    }
    return items, counts


def submit(cfg: ChinookConfig, items: list[dict]) -> str:
    """Submit the mirror document. The destructive flags live here, in one place."""
    return globus.submit(
        cfg,
        items,
        source=cfg.local_endpoint,
        destination=cfg.remote_endpoint,
        label=BACKUP_LABEL,
        # 2 = compare mtime+size. Unlike the content-addressed cache objects,
        # ordinary workspace files are mutated in place, so size alone misses a
        # same-length edit.
        sync_level=2,
        # The whole point: this remote path is a mirror, not an accumulator.
        delete_destination_extra=True,
        # A 100k-file scan will always race some file being rewritten or removed
        # underneath it; one vanished file must not fail the backup.
        skip_source_errors=True,
    )


def backup(*, dry_run: bool = False) -> dict[str, Any]:
    """Build the mirror document and submit it, returning the task id.

    Single-flight is *not* enforced here — it is a partial unique index in the
    run table (:mod:`awm.dvc.runs`), because the scheduler, the verb, and the
    console script are three processes and an in-process lock only ever guarded
    one of them. Callers go through :mod:`awm.dvc.jobs`.
    """
    from awm.dvc.config import load

    cfg = load()

    items, excluded = build_items(cfg)
    result: dict[str, Any] = {
        "items": len(items),
        "excluded": excluded,
        "destination": f"{cfg.remote_endpoint}:{destination_root(cfg)}",
        "mirror": True,
    }
    if dry_run:
        return {**result, "dry_run": True, "transfer_items": items}

    task_id = submit(cfg, items)
    return {**result, "submitted": True, "task_id": task_id}
