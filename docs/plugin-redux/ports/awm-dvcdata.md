# Port plan: awm-dvcdata

## Purpose & Contents

This file holds the approved port plan for awm-dvcdata, from the ops group's plan (task CWPYAtyb). Tony approved it on 2026-11-06. It is the plan as approved: later changes go in the port commits and the journal.

## Rules common to every ops port

- Each port commit adds `core/plugins/<name>/` and deletes the Python folder in the same commit. That folder holds `awm-plugin.toml`, the crate `awm-plugin-<name>` with binary `<name>`, and any pages.
- Each port removes the plugin's rows from `scripts/run-tests.sh` and adds, in the same commit, a `run.sh` that is one line, `exec` of the binary, and the binary's `[artifacts]` pin line in `deploy.toml`, in the pin format of PLAN.md T2.
- Build and test only through `scripts/rust-build.sh` with `AWM_CARGO_TARGET_DIR=$HOME/.cache/awm-target/port-ops`.
- Submit with `scripts/land.sh submit --brief <file> --restart <plugin>`. Then run the live check on mizar and on one other node.
- SQLite uses `rusqlite` with `bundled`, through the knowledge group's store crate (`awm-store`) when it has landed. Otherwise the plugin opens the database directly with the same path rule and switches to the store crate later.
- The SDK needs listed per plugin go to plugin-redux/interview. No plugin builds a private copy.

## Plan

**Replaces.** `awm/service_components/dvcdata/` (data_dvc 1450, cachetag 28, `__init__` 7; tests 859) plus dvc's `cache.py` (187), which folds in. dvc and scopes import dvcdata (scopes is a knowledge-group port). The Python library stays until the later of the dvc and scopes ports lands. That port deletes it.

**What changes in kind.** Python calls the `dvc` CLI (DVC 3.67.1) for:
- `config cache.type`;
- `checkout`;
- `status`;
- `data status --granular --json`;
- `status --json` (scopes' drift check);
- `git-hook merge-driver`;
- the three hook bodies.

The crate does all of this natively. Already native in Python, and ported as is: pin YAML, cache paths, `.dir` reading, CACHEDIR.TAG, shared chunks, mount lists, the sentinel, and the keep-set gc. After the port no plugin runs the DVC CLI. It stays a hand-run node tool for authoring data (`dvc add`, `unprotect`).

**Format facts, checked on mizar.**
- The cache is DVC 3 `files/md5/<2>/<30>`, with objects at mode 0444 and hardlinked.
- A file's id is the plain md5 of its bytes, with no dos2unix.
- A `.dir` id is the md5 of the manifest bytes, plus the suffix `.dir`.
- The manifest bytes are exactly Python `json.dumps(sorted, sort_keys=True)`: `", "` and `": "` separators, ASCII-escaped, sorted by relpath, entries `{md5, relpath}`.
- Every pin on mizar has `hash: md5`.

**API (modules).**
- `paths`: constants and the workspace, cache, project, state-dir and shared-store paths. `is_dvc_project` loses its binary check.
- `cachetag`.
- `git`: `run`, `common_dir`, `chunk_pins`, `worktrees`, `head_rev`, `current_branch` and `check_ignored`.
- `pin`: parse, `pin_md5`, `out_hashes`, `find_pins` and `is_legacy`. A legacy 2.x pin is refused, never hashed.
- `cache`: `object_path`, `read_manifest`, `manifest_bytes`, `manifest_id`, `write_object` (temp file, then 0444, then rename), `md5_file`, `resolve` and `open_scope`.
- `config`: `ensure_cache_config` (a native INI edit) and `cache_dir_for`.
- `checkout`, with DVC 3 semantics:
  1. An inode match counts as unchanged with no hashing. Otherwise compare size, then md5.
  2. Delete workspace files the pin no longer lists.
  3. With force=false, refuse to replace a file whose content is not in the cache. awm never passes force.
  4. Link hardlink, then symlink.
  5. On a link failure, remove the whole out.
- `status`: `changed_outs`, using DVC's state vocabulary, `in_sync`, and `data_status_json`, which keeps the same keys.
- `shared`, `mounts` and `perms`.
- `wiring`: `ensure_repo_wiring(repo, hook_bin)`. It writes the merge driver `<hook_bin> merge-driver %O %A %B`, `*.dvc merge=dvc`, and three shim hooks that exec `<hook_bin> hook <name>`. It never clobbers a hook that lacks the marker.
- `hook`: reproduces `_CHECKOUT_SH` exactly: the mount list, shared repointing, LINKS_ONLY, and the sentinel text.
- `merge`: reproduces DVC's tree merge, and rewrites only the md5, size and nfiles lines of `%A`.
- `provision` and `report`. The report keys stay the same, except that `dvc_bin` goes and `engine` comes.
- `gc`: a verbatim port with every safety rule:
  - only `all-commits` is accepted;
  - every wired project must be listed or excluded;
  - the keep-set covers history, the staged index and modified pins;
  - every named manifest is expanded;
  - objects get a 14-day grace;
  - the layout must be exactly `files/md5`;
  - the run holds a flock on `<cache>.gc.lock`;
  - it never takes a DVC repo lock;
  - dry-run is the default at the verb.

**The scopes interface (cross-group, knowledge).** scopes uses these, each with a crate replacement:
- `MOUNTS_FILE`, `CHECKOUT_FAILED_FILE`, `state_dir`, `is_dvc_repo`, `is_dvc_project`, `SHARED_STORE_DIRNAME`;
- `ensure_repo_wiring(...).actions`;
- `chmod_dirs_writable`;
- `chunk_pins`;
- `provision_scope_data`;
- the private `_dvc(wt, "status", "--json")`, which becomes `status::changed_outs`.

`dvc_available()` is removed: it is always true now. `status::changed_outs` is the drift check. The crate never locates or runs a `dvc` binary, so it needs no `sys.prefix` or env lookup. scopes and dvc must pass the same `hook_bin`, the dvc binary's stable path. Otherwise each one rewrites the other's hooks.

**State.** The crate keeps no store of its own. It reads and writes the workspace data layer in place: the cache, the gc lock, per-project caches, the shared store, `.dvc/config` and `config.local`, pins, `.awm-shared`, `<git-dir>/awm/data-mounts` and the sentinel, the hooks, `info/attributes` and `merge.dvc.*`. Migration: on the first wiring after the port, each AWM-managed hook is copied once to `<hook>.awm-py.bak`, then rewritten to the shim form. Foreign hooks stay untouched.

**Rust crates added.** `md-5` (pure Rust) and, optionally, `walkdir`. It uses no git library: it shells out to `git`, as Python does.

**Test plan.**
1. Port the 43 `test_data_dvc` tests and the 12 `test_cache` tests, with fixtures built natively.
2. Round-trip every real `.dir` object on mizar and one backup node.
3. Check non-ASCII and astral relpaths against fixed Python bytes.
4. Check that a CRLF file hashes as plain md5.
5. Interop tests, `#[ignore]` unless `AWM_DVC_BIN` is set (mizar's dvc env):
   - a native checkout of a tree that `dvc add` made gives a clean `dvc status`;
   - the native merge driver and DVC's give byte-identical results on add, remove, change and conflict;
   - `changed_outs` equals `dvc status --json`.

**Landing.** One commit adds the crate. It runs no plugin and restarts nothing. It lands before the dvc and scopes ports.

Gotchas:
- A native checkout that diverges from DVC can delete worktree data. The interop suite and the unsaved-file refusal guard it.
- A manifest serialization mismatch writes ids that DVC cannot find. The whole-cache round-trip must pass before landing.
- While scopes is still Python, its wiring writes Python-dvc hooks and Rust dvc writes shims. Both work, but each rewrites the other on provision. Land the dvc and scopes ports close together.
- Survey every node's bare repos for legacy pins without `hash:` before landing.
