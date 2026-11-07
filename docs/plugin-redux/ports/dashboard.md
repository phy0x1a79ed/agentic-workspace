# Port plan: dashboard

## Purpose & Contents

This file holds the approved port plan for dashboard, from the ops group's plan (task CWPYAtyb). Tony approved it on 2026-11-06. It is the plan as approved: later changes go in the port commits and the journal.

## Rules common to every ops port

- Each port commit adds `core/plugins/<name>/` and deletes the Python folder in the same commit. That folder holds `awm-plugin.toml`, the crate `awm-plugin-<name>` with binary `<name>`, and any pages.
- Each port removes the plugin's rows from `scripts/run-tests.sh` and adds, in the same commit, a `run.sh` that is one line, `exec` of the binary, and the binary's `[artifacts]` pin line in `deploy.toml`, in the pin format of PLAN.md T2.
- Build and test only through `scripts/rust-build.sh` with `AWM_CARGO_TARGET_DIR=$HOME/.cache/awm-target/port-ops`.
- Submit with `scripts/land.sh submit --brief <file> --restart <plugin>`. Then run the live check on mizar and on one other node.
- SQLite uses `rusqlite` with `bundled`, through the knowledge group's store crate (`awm-store`) when it has landed. Otherwise the plugin opens the database directly with the same path rule and switches to the store crate later.
- The SDK needs listed per plugin go to plugin-redux/interview. No plugin builds a private copy.

## Plan

**Replaces.** `awm/plugins/dashboard/`: the manifest and three Svelte pages (dashboard, settings, gallery), 1154 lines with no Python. It has no service and no crate. A folder that holds only a manifest and pages is a valid plugin.

**Surface.** The pages stay at `/p/dashboard/<page>/`. They call only the gateway's native `awm config_contracts` and `awm config_set`. The gallery calls nothing. The settings card comes from auth's `config` block (T9).

**The move.** `git mv` to `core/plugins/dashboard`, and fix the gallery path in the primitives comment. The commit relies on two pieces of plugin-redux/interview's PLAN.md T2, which land before it:
- The page pipeline for `core/plugins`: `build.sh`, `release-pages.sh`, `tsconfig.pages.json`, `.gitignore`, and `scripts/pins.sh` `pages_version` hashing `core/plugins/*/pages/*`.
- The core pin, which excludes `core/plugins` (891e8692).

**Test plan.**
1. The build lists the same three pages from the new path.
2. The release tarball holds `core/plugins/dashboard/...` and no `awm/plugins/dashboard`.
3. A page edit changes `pages_version`.
4. On mizar and alcor, the three pages and their assets return 200, with hashes equal to the pre-move build.
5. The settings card round-trips a save.

**Landing.** Land after the `core/plugins` gateway root and the PLAN.md T2 page pipeline are live on every node. No restart is needed: a reload picks it up.

Gotchas: if the pages pin misses `core/plugins`, push nodes keep serving the old dist. Test step 3 catches that.
