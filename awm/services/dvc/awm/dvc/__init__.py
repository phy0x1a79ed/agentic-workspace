"""AWM dvc service — off-site backup of this workspace, on two deliberate paths.

* :mod:`awm.dvc.sync` — the shared DVC cache to chinook, **append-only**. An
  archive: it never deletes, so a local ``dvc gc`` stays recoverable.
* :mod:`awm.dvc.backup` — everything else, as a **mirror** that deletes. The
  DVC checkouts are excluded because the archive already holds their bytes,
  which is the one coupling between the two paths.

They write to sibling roots on the collection (``<prefix>/data/.dvc_cache/`` and
``<prefix>/workspace/``) so the destructive one cannot reach the other.

:mod:`awm.dvc.transfer` is the archive's hash-selective inverse, moving only the
cache objects a single scope pins; :mod:`awm.dvc.cache` resolves pins to object
hashes (local only); :mod:`awm.dvc.globus` submits transfer documents and reports
task state; :mod:`awm.dvc.coverage` reports what neither path holds.
"""
