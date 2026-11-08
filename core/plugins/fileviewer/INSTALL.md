# The `fileviewer` plugin

## Purpose & Contents

This file covers what the `fileviewer` mount exposes, how its mask hides secrets, and how to check it on a node. The plugin is one static binary, `fileviewer`, which the deploy places at `<AWM_DIR>/bin/fileviewer`. It needs no install step and keeps no state.

## The mount

The service serves files on a loopback listener and registers it as its mount. The gateway serves it at `/s/fileviewer/<absolute path>`, and the HTTPS front fronts the gateway, so a link is origin-relative and renders on any device that can open the portal. Peers never reach the listener: the edge carries only tool calls.

- Each file goes out with the `Content-Type` that Python 3.12's `mimetypes` gave it: the built-in table, then the system `mime.types` files.
- A directory serves its `index.html`, and never a listing.
- A missing path, a path outside the root, a masked path and a symlink loop all return the same plain 404.

## The mask

The mount exposes every file under the root, so a deny list hides secrets. Each glob matches the whole symlink-resolved path relative to the root, so a symlink to a secret is hidden too. `**` spans directories. A leading `!` re-exposes, and the last matching glob wins. The default list is `DEFAULT_MASK` in `src/mask.rs`.

`FILEVIEWER_MASK_FILE` adds globs, one per line, after the defaults. The file hides itself.

**CAUTION** A glob that does not compile, or a mask file that is not UTF-8, hides every path. The service log names the bad glob.

**CAUTION** The mask is a deny list. A new kind of secret in a place the list does not name is exposed until a glob covers it. Narrow `FILEVIEWER_MOUNT_ROOT` to bound what a gap can expose.

## Environment

| Var | Default | Effect |
|---|---|---|
| `FILEVIEWER_MOUNT_ROOT` | `/` | the filesystem root the mount exposes |
| `FILEVIEWER_PORT` | a free port | the loopback port of the listener |
| `FILEVIEWER_MASK_FILE` | none | extra deny globs |

## Verify

1. Run `awm fileviewer status`. It names the prefix, the root, the listener and the number of deny globs.
2. Fetch a real file through the front:
   `curl -sk -o /dev/null -w '%{content_type} %{http_code}\n' "https://127.0.0.1:12100/s/fileviewer/<absolute path>.png"` prints `image/png 200`.
3. Fetch a masked file, such as `/s/fileviewer/home/<user>/.ssh/id_ed25519`. It prints `404`.
4. Open a figure URL in a browser on another device, and check that it renders.
