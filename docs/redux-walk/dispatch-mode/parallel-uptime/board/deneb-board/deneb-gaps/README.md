# deneb-gaps

## Purpose & Contents

This topic holds the three awm gaps that deneb-board's dry run found.

## Result

Solved in awm release f5c5294d (deneb-singletons 04ec7d9f): the trilium service refuses an empty vault and `awm trilium init` creates one; zotero writes its library as an attachment through the trilium tool; each singleton reads its own secrets through LoadCredential. core-artifact in the same release ships the core and pages as pins, so deneb builds nothing.
