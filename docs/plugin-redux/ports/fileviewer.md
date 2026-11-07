# Port plan: fileviewer

## Purpose & Contents

This file holds the approved port plan for fileviewer, from the ops group's plan (task CWPYAtyb). Tony approved it on 2026-11-06. It is the plan as approved: later changes go in the port commits and the journal.

## Rules common to every ops port

- Each port commit adds `core/plugins/<name>/` and deletes the Python folder in the same commit. That folder holds `awm-plugin.toml`, the crate `awm-plugin-<name>` with binary `<name>`, and any pages.
- Each port removes the plugin's rows from `scripts/run-tests.sh` and adds, in the same commit, a `run.sh` that is one line, `exec` of the binary, and the binary's `[artifacts]` pin line in `deploy.toml`, in the pin format of PLAN.md T2.
- Build and test only through `scripts/rust-build.sh` with `AWM_CARGO_TARGET_DIR=$HOME/.cache/awm-target/port-ops`.
- Submit with `scripts/land.sh submit --brief <file> --restart <plugin>`. Then run the live check on mizar and on one other node.
- SQLite uses `rusqlite` with `bundled`, through the knowledge group's store crate (`awm-store`) when it has landed. Otherwise the plugin opens the database directly with the same path rule and switches to the store crate later.
- The SDK needs listed per plugin go to plugin-redux/interview. No plugin builds a private copy.

## Plan

**Replaces.** `awm/services/fileviewer/` (server 298, hub_adapter 85, `__init__` 15 = 398 src; test 174). It uses gatewayclient only.

**Tool surface.** Tool `fileviewer`, major 1, tier off.

| Verb | Params | Surfaces | Purpose |
|---|---|---|---|
| status | none | default | `mounted, prefix, root, deny_globs, listen, url_shape`. Never fails. |

**HTTP mount** at `/s/fileviewer/<path>`:
- It accepts GET and HEAD.
- The path is percent-decoded like `unquote`. A NUL is 404.
- The path is realpath-resolved under `FILEVIEWER_MOUNT_ROOT` (default `/`). A path outside the root is 404.
- The mask is checked on the resolved path, so a symlink to a secret is caught.
- A directory serves its `index.html` only, never a listing.
- A file that is not regular is 404.
- Every refusal is the same plain `not found` 404. Other methods get 501.
- The mask is 31 default globs, kept byte for byte and in order. `FILEVIEWER_MASK_FILE` adds lines and hides itself. Negation works gitignore-style, and the last match wins. The glob-to-regex translation matches `_glob_regex`.
- Content types come from a port of Python 3.12's `mimetypes` table, overlaid by `/etc/mime.types`, never from `mime_guess`.

**Changes to the surface.**
1. A symlinked `index.html` is resolved and checked again. This closes a hole: Python serves a masked or outside target through it.
2. The final open uses `O_NOFOLLOW`.
3. A path starting with `//` resolves as written.
4. A symlink loop is 404 instead of a dropped connection.
5. A bad mask glob denies everything and logs why.
6. The 501 body and the `Server` header differ.

**State.** None. **Dependencies.** `platform = ["linux"]`, no needs, no calls. Callers: Tony's browser and `awm fileviewer status`. auth's public policy keeps the `/s/fileviewer/projects/userdata/<user>` rule.

**Rust crates added.** `regex`, `percent-encoding`, `tokio-util` (to stream the body), `tower` (dev).

**SDK needs.** The mount accepts a chosen port or a pre-bound listener. The `status` handler can read the bound mount URL.

**Test plan.**
1. Port the `test_server` tests: the mask list, the 17 glob cases, the mask-file merge and self-hide, the status shape, and the listener matrix through raw URIs.
2. New tests: a symlinked `index.html` (to a secret, and outside the root), negation order, POST, NUL, and the mime parity list captured on mizar.
3. Live check on mizar (root `/`) and deneb (root `/var/lib/awm`):
   - status minus `listen`;
   - a curl matrix of codes, content types and body sha256: file types, index dirs, secrets, `.git/config`, an encoded `..`, a symlink to a `.pem`, HEAD and POST;
   - on deneb, a userdata file through the front.
   Diffs are allowed only for `listen` and the six listed changes.

**Landing.** One commit. Restart `fileviewer`. Then check `awm fileviewer status` shows `mounted: true` with 31 globs, re-run the curl matrix, and open one figure in a browser.

Gotchas: deneb runs fileviewer as the `awm` user with its root from the install config `[env]`. The binary must reach deneb's `/opt/awm`. The plugin loads only once the gateway's core roots include `core/plugins` and the workspace members include `plugins/*`.
