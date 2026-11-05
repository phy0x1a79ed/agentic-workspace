# zotero-home

## Purpose & Contents

This topic holds where zotero runs. redux-build/dev raised it after excluding zotero on mizar refused kb and 10 more plugins for 3 minutes (2026-11-05, reverted as cloud a7a2694).

## Result

zotero becomes a deneb singleton in edges.yml, the one writer of the library, with every kb node as a caller. It runs as its own user with its own credential, as trilium does. Until door-client is live, mizar keeps zotero loaded with ZOTERO_SYNC_ENABLED=0 in a temporary drop-in. That drop-in and zotero on every node but deneb go after the door goes live.
