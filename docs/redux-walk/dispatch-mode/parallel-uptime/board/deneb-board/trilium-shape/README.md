# trilium-shape

## Purpose & Contents

This topic holds how Trilium runs on deneb. redux-build/deneb-board raised it.

## Result

Build the end state of fleet.md R105 item 1, with no intermediate shape. Trilium runs as a singleton, `awm-plugin@trilium` under its own user awm-trilium, with the R99 sandbox. The trilium plugin's install unpacks the pushed, pinned tarball. The data lives in /var/lib/awm/trilium/data, and the passkey login, backup and nightly copy point there. Moving the live vault is a migration step: stop, consistent backup to mizar, move with ownership, start, check dbVersion 240 and the note count. Tony approves it in Q-deneb.
