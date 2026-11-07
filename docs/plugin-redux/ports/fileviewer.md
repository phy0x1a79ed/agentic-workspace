# Port plan: fileviewer

## Purpose & Contents

This file holds the approved port plan for fileviewer, from task T3 of the ops group's plan (task CWPYAtyb), revised and approved on 2026-11-06. It names only what changes. The Python code and its manifests are the reference for everything the port keeps. The rules that hold for every ops port are in [README.md](README.md). Later changes go in the port commits and the journal.

## Port fileviewer

The one verb, `status`, keeps its shape. The mount `/s/fileviewer/<path>` keeps:
- GET and HEAD;
- realpath containment under `FILEVIEWER_MOUNT_ROOT`;
- the mask globs, in order, plus `FILEVIEWER_MASK_FILE`;
- `index.html` only, never a listing;
- one uniform 404;
- Python 3.12's `mimetypes` table.

Changes:
1. A symlinked `index.html` is checked again, which closes a hole.
2. The final open uses `O_NOFOLLOW`.
3. A path starting with `//` resolves as written.
4. A symlink loop returns 404.
5. A bad mask glob denies everything.
6. The 501 body and the `Server` header differ.
7. The mask gains `**/.pre-rust/**`, `**/.rust-*/**` and `**/globus-tokens.json`.

fileviewer has no state and no needs.

Tests: port `test_server`, and add the seven changes and a mime parity list. The live check on mizar (root `/`) and deneb (root `/var/lib/awm`) runs a curl matrix of codes, types and body hashes. Only the seven changes may differ. On deneb, the binary must reach `/opt/awm`.

