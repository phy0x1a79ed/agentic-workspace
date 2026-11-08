# Port plan: awm-statecheck

## Purpose & Contents

This file holds the approved plan for awm-statecheck, a new crate, from task T14 of the ops group's plan (task CWPYAtyb), approved on 2026-11-06. The rules that hold for every ops port are in [README.md](README.md). Later changes go in the port commits and the journal.

## Build awm-statecheck and inventory each node's pre-redux state

This crate is the one state check for every group, and plugin-redux/interview forwarded its shape to the other groups. It lands right after T1, before any plugin.

API:
- `Decl::load(plugin_dir)` reads `state.toml`. With no file, it copies the default state dir whole.
- `snapshot_once(state_dir, plugin, &Decl)` snapshots once into `<state dir>/.pre-rust/`:
  - It writes into `.pre-rust.tmp/` and renames that to `.pre-rust/` only after every item is copied and fsync'd. A cut-short snapshot (the gateway's orphan reaper, a crash, a full disk) is therefore never taken as complete. A leftover `.pre-rust.tmp/` is redone, never trusted.
  - It creates the tmp dir at mode 0700 and writes a valid `CACHEDIR.TAG` there before anything else. dvc's off-site mirror then skips it.
  - It copies each database with `VACUUM INTO`, so free pages and old plaintext stay behind. It never copies a live WAL database file by `cp`.
  - It copies other files with their owner and mode.
  - It never overwrites an existing `.pre-rust/`.
  - An absent or empty state dir gets an inventory-only `.pre-rust/`.
  - It refuses when free disk is below the snapshot's size plus 10%. `size_only` items do not count, so kb's 170 GB on altair is recorded, not copied.
  - The SDK calls it in `Service::run` before `on_start`. A failure refuses the start. The `state snapshot` subcommand lets the deploy step take the copy before the restart.
- `inventory` lists paths, modes, owners, sizes, and per database its tables, columns and row counts. It never lists contents.
- `check` names every table, column, row key or file that `.pre-rust/` holds and the live state lost. Declared additive and transient items pass.
- `restore` is a damage-only action. It refuses without `--damage-proven`, and it prints that the rollback rule is to revert the code and keep the live state. It runs with the unit stopped, takes the stores to restore by name, moves each one aside to a tagged `.rust-<ts>/`, and deletes nothing.
- `prune` deletes `.pre-rust/`, only with Tony's dated OK.
- Each operation is also a `<plugin> state …` subcommand for operators.

`state.toml`:
- `dirs` lists the state dirs, with `${VAR:-default}` expansion.
- `[[additive]]` (path, table, column) lists what the port adds.
- `[[transient]]` (path, table) lists rows or files that may vanish.
- `[[size_only]]` (path) records size and mtime only.
- `[[extra]]` (name, path) copies state outside the dirs to `.pre-rust/extra/<name>/`.

A `size_only` or `extra` path may lie outside every state dir. The crate writes nothing into that tree. Its record lands in the first state dir's `.pre-rust/`.

| Plugin | State (`dirs`, `extra`) | Additive | Transient |
|---|---|---|---|
| fileviewer, dashboard | none | none | none |
| routine | `services/routine/`, `<AWM_DIR>/land/`, the `awm-routine-*` units, `services/events/` | `timer_slots` | `lease.json`, `lock` |
| dvc | `services/dvc/`, `<AWM_DIR>/dvc.toml`, each wired repo's hooks and merge config | `globus-tokens.json`, shim hooks | none |
| auth | `services/auth/`, `services/httpsfront/`, `awm/services/httpsfront/.certs`, and the CA dir (inventory only) | `landing.db`, `<state>/certs`, `<state>/sans` | none |
| operations | `$HOME/.awm/operations/` (queue, relay, approver, keys, config), the Arbutus pending dir | `tasks.body`, `ops_notes`, kv `vault_mark` and `board_import` | relay `outbox`, `seen_orders`, `cache` |

Orphans and their fates. No key is deleted without Tony's OK.

| Orphan | Where | Fate |
|---|---|---|
| httpsfront `.certs/key.pem`, `.sans` | six nodes; `.sans` on capella | copied by auth, original left in place, Tony decides |
| claude-science `.certs` | alcor, capella, altair | left after C2b, Tony decides |
| `.runtime-env`, egg-info in old plugin folders | each node | deleted with the Python folder (regenerable, no secret) |
| auth `node_ed25519.key`, `peer_cred.current` (unused since v7) | auth state dir | moved to `.rust-<ts>/` at the land, Tony decides |
| the operations unit's miniforge bind | alcor | removed by the role change |
| `services/events/` and its unmerged WAL | each routine node | snapshotted, left unread, Tony decides at prune |
| the globus CLI `storage.db` | dvc nodes | left for the CLI, Tony decides after dvc's acceptance |
| empty `~/.awm/penpot` dirs | some nodes | Tony decides at prune |

Reading root-owned state: on alcor and deneb, run the inventory as `sudo -u <unit user> <binary> state inventory` in an admin window, with the approver's ssh grant. On the other nodes, run it as the plugin user. Run it on mizar, alcor, capella, altair and deneb, for the plugins each node runs.

Retention: `.pre-rust/` stays through an acceptance window of 14 days after the operations land. At debrief, file a `request_human` review dated to the window's end. Run `state prune` on each node only on Tony's OK at that time.

Tests:
- A synthetic WAL database round-trips through snapshot, check and restore.
- `VACUUM INTO` leaves no deleted row's text in the copy.
- A lost row, column or file fails the check. An additive or transient item passes.
- The tag is written before the first file.
- An interrupted snapshot leaves only `.pre-rust.tmp/`, and the next run redoes it.
- The snapshot refuses a second copy and a disk below size plus 10%.
- `restore` refuses without `--damage-proven`.
- The inventory holds no cell value.
- An outside `size_only` tree stays byte-identical.

Gotchas:
- On alcor, operations' `.pre-rust/` sits inside `/var/lib/awm/operations/.awm/operations/`, within the unit's writable paths.
- `check` skips `.pre-rust/` and `.rust-*/`.

