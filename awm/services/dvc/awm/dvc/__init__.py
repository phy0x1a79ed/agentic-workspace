"""AWM dvc service — DVC cache sync between this workspace and chinook.

Two directions over one Globus collection:

* :mod:`awm.dvc.mirror` — the daily full-workspace backup (destructive mirror).
* :mod:`awm.dvc.transfer` — its hash-selective inverse, moving only the cache
  objects a single scope pins.

:mod:`awm.dvc.cache` resolves pins to object hashes (local only);
:mod:`awm.dvc.globus` submits transfer documents and reports task state.
"""
