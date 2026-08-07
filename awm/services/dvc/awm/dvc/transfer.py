"""Hash-selective pull/push of DVC cache objects against chinook.

The nightly ``mirror`` covers the push direction for the whole workspace, but has
no inverse: restoring by re-mirroring is all-or-nothing against an 86 GB / 53k-
object cache, which is the wrong tool for "this scope is missing three pins".
This module is that inverse — resolve one scope's pins to their exact object set,
diff against the local cache, and move only the difference.

**Pull is idempotent and self-advancing, not blocking.** A cold restore needs
``.dir`` manifests in hand before their leaves can even be named (see
:mod:`awm.dvc.cache`), so it takes two Globus tasks. Rather than hold an RPC open
between them, each ``pull`` call does as much as is currently knowable and reports
which phase it is in; calling it again after the manifest task lands re-resolves
and proceeds to the leaves. Same call, no state to carry, safe to repeat.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from awm.dvc import cache as cachemod
from awm.dvc import globus
from awm.dvc.config import WORKSPACE_ROOT, ChinookConfig

log = logging.getLogger("awm.dvc.transfer")


def transfer_items(
    hashes: set[str], cache: Path, cfg: ChinookConfig, *, direction: str
) -> list[dict]:
    """One transfer item per object, addressed by explicit path.

    Never ``filter_rules``: Globus filters match an item's *name* at any depth,
    which cannot express "this exact object" — the same limitation that forces
    :mod:`awm.dvc.mirror` to partition by path.
    """
    rel_cache = cache.relative_to(WORKSPACE_ROOT)
    items = []
    for h in sorted(hashes):
        rel = f"{rel_cache}/{cachemod.object_relpath(h)}"
        local = str(WORKSPACE_ROOT / rel)
        remote = f"{cfg.prefix}/{rel}"
        src, dst = (remote, local) if direction == "pull" else (local, remote)
        items.append(
            {
                "DATA_TYPE": "transfer_item",
                "source_path": src,
                "destination_path": dst,
                "recursive": False,
            }
        )
    return items


def _submit(
    cfg: ChinookConfig, items: list[dict], *, direction: str, label: str
) -> str:
    src, dst = (
        (cfg.remote_endpoint, cfg.local_endpoint)
        if direction == "pull"
        else (cfg.local_endpoint, cfg.remote_endpoint)
    )
    return globus.submit(
        cfg,
        items,
        source=src,
        destination=dst,
        label=label,
        # 1 = copy when size differs. Objects are immutable and content-addressed,
        # so a same-size file IS the right file; this still re-copies anything a
        # previously interrupted task left truncated. Checksum-level would mean
        # hashing both sides of an 86 GB store to learn nothing.
        sync_level=1,
        # Never. A restore must not be able to prune either side — and chinook is
        # already pruned destructively by the mirror, which is the one job that
        # gets to do that.
        delete_destination_extra=False,
    )


def status(scope_path: str) -> dict[str, Any]:
    """Report a scope's pin/object state against the local cache. No network."""
    scope, cache = cachemod.open_scope(scope_path)
    res = cachemod.resolve(scope, cache)
    out = {
        "scope": str(scope),
        "cache": str(cache),
        "pins": res.pins,
        "manifests": len(res.manifests),
        "unresolved_manifests": len(res.unresolved),
        "objects_known": len(res.objects),
        "present": len(res.present),
        "missing": len(res.missing),
        "complete": not res.missing and not res.unresolved,
    }
    if res.unresolved:
        out["note"] = (
            f"{len(res.unresolved)} manifest(s) absent locally — their leaves are "
            "not counted above. pull fetches manifests first, then the leaves."
        )
    return out


def resolve(scope_path: str) -> dict[str, Any]:
    """The explicit object-hash set a scope depends on. No network."""
    scope, cache = cachemod.open_scope(scope_path)
    res = cachemod.resolve(scope, cache)
    return {
        "scope": str(scope),
        "cache": str(cache),
        "objects": sorted(res.objects),
        "present": sorted(res.present),
        "missing": sorted(res.missing),
        "unresolved_manifests": sorted(res.unresolved),
        "complete": not res.unresolved,
    }


def pull(scope_path: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Fetch the objects a scope needs but does not have.

    Phase 1 fetches any absent ``.dir`` manifests; phase 2 fetches the leaves they
    reveal. One call advances one phase — call again once the returned task
    completes to advance the next.
    """
    from awm.dvc.config import load

    cfg = load()
    scope, cache = cachemod.open_scope(scope_path)
    res = cachemod.resolve(scope, cache)

    if res.unresolved:
        items = transfer_items(res.unresolved, cache, cfg, direction="pull")
        result = {
            "scope": str(scope),
            "phase": "manifests",
            "objects": len(items),
            "note": (
                "leaves are unknowable until these manifests are local — call pull "
                "again once this task completes to fetch them"
            ),
        }
        if dry_run:
            return {**result, "dry_run": True, "items": items}
        result["task_id"] = _submit(
            cfg, items, direction="pull", label=f"dvc pull manifests {scope.name}"
        )
        return result

    if not res.missing:
        return {
            "scope": str(scope),
            "phase": "complete",
            "objects": 0,
            "note": "nothing to pull — every object this scope pins is already local",
        }

    items = transfer_items(res.missing, cache, cfg, direction="pull")
    result = {
        "scope": str(scope),
        "phase": "objects",
        "objects": len(items),
        # A pulled object is a fresh inode at mode 0644 — not the read-only,
        # many-times-hardlinked file DVC leaves behind. Nothing reconnects it to
        # the worktree on its own, so checkout is required, not merely suggested.
        "note": "on completion, run `dvc checkout` in the scope to materialize",
    }
    if dry_run:
        return {**result, "dry_run": True, "items": items}
    result["task_id"] = _submit(
        cfg, items, direction="pull", label=f"dvc pull {scope.name}"
    )
    return result


def push(scope_path: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Send the objects a scope pins up to chinook."""
    from awm.dvc.config import load

    cfg = load()
    scope, cache = cachemod.open_scope(scope_path)
    res = cachemod.resolve(scope, cache)

    if res.unresolved:
        raise cachemod.DvcScopeError(
            f"{len(res.unresolved)} manifest(s) missing locally — cannot push what "
            "isn't here; pull first"
        )
    if not res.present:
        return {"scope": str(scope), "objects": 0, "note": "nothing to push"}

    items = transfer_items(res.present, cache, cfg, direction="push")
    result: dict[str, Any] = {
        "scope": str(scope),
        "objects": len(items),
        "skipped_absent": len(res.missing),
    }
    if dry_run:
        return {**result, "dry_run": True, "items": items}
    result["task_id"] = _submit(
        cfg, items, direction="push", label=f"dvc push {scope.name}"
    )
    return result
