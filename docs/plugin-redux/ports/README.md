# Port plans

## Purpose & Contents

This folder holds one port plan per plugin or shared crate of the plugin redux, plus the `describe` baselines the ports are checked against. A plan names only what the Python does not make obvious: the byte formats and behaviours to keep exactly, the deliberate changes, and the tests. The Python at release 49232bf9 is the specification for everything else.

- `<name>.md`: the port plan for a plugin or a shared crate.
- `HANDOFF-<group>.md`: a group's unlanded work, for the merge session. Delete it once that work lands.
- `baseline/<tool>.describe.json`: each tool's `describe` at release 49232bf9. A port's describe must equal it, apart from the changes its plan lists.
- `<group>-handoff.md`: what a group's land session needs: branch heads, land order, rollout steps and checks.

Delete a plan once its port has landed and its crate's own README carries what still matters.

## Ops group

The ops group's plans (task CWPYAtyb) were approved on 2026-11-06. Order: awm-trust, awm-statecheck, awm-dvcdata, fileviewer, dashboard, routine, dvc, auth, operations. These rules hold for every port of the group.

- **Layout.** A port commit adds `core/plugins/<name>/`. It holds the manifest, `state.toml` (T14), a one-line `run.sh` that execs `$AWM_DIR/bin/<name>`, and the crate `awm-plugin-<name>`. The same commit:
  - adds the pin to `deploy.toml` `pins`;
  - removes the plugin's `scripts/run-tests.sh` rows;
  - deletes the Python folder.
- **Build.**
  - Build and test only through `scripts/rust-build.sh`, with `AWM_CARGO_TARGET_DIR=$HOME/.cache/awm-target/port-ops`.
  - Merge `feat/plugin-redux` from mizar's bare repo (not `origin/`, which is stale), never rebase, each time plugin-redux/interview says it moved and before each port commit.
  - On ENOSPC, stop, tell plugin-redux/interview and delete nothing.
- **Crates.** Pure Rust and musl-friendly: rustls with ring, and `rusqlite` bundled (through knowledge's `awm-store` once it lands). No openssl and no webauthn-rs.
- **State safety.**
  1. Before a commit deletes a Python folder, run `git -C projects/awm/.bare log 49232bf9..release -- <folder>`. That is mizar's bare repo, because this scope's origin, altair, has a stale `release`. Carry each commit it lists into the port.
  2. Inventory each node's state before the first land (T14).
  3. The first Rust start snapshots the state (T14).
  4. Keep migrations additive, and never rewrite a config file the user owns.
  5. Use synthetic ids in fixtures, tests and logs. Live parity captures stay in the job's tmp dir at 0600, are compared by sha256 and are then deleted.
  6. Fail closed. A plugin whose key, CA or database is missing where one existed refuses to start, and never mints or creates a fresh one.
- **Rollback.** Revert the code and keep the live state files. Restore a store from `.pre-rust/` only when `state check` proves damage, and only as each port's section allows. A wholesale restore of a security store is unsafe:
  - restoring `auth.db` revives revoked tokens;
  - restoring `approver.db` rewinds the device chain, and `stale_head` then locks Tony out;
  - restoring `relay.db` replays spawn orders;
  - restoring the land dir revives old leases.
- **Caller gate.** A verb that hands out a secret, or acts with alcor's authority, checks the calling plugin from the call frame, and not only `as_`. Each such verb carries an allow-list constant with a test. The Python auth and operations gate on the same frame field since 1f250d05, and the Rust allow-lists match theirs.
- **State dir.** Each port reads its pre-redux paths, as its `state.toml` names them. A plugin with no override uses `<AWM_DIR>/services/<service>`, the SDK's `Env::state_dir`. `<AWM_DIR>/plugins/<plugin>/` holds only the gateway's `install.stamp`.
- **Gate.**
  - Land no plugin before plugin-redux/interview says the SDK has landed.
  - Before each `land.sh submit`, send plugin-redux/interview the brief, the plugin's inventory and a `state check` run on a copy.
  - Submit only after its OK, with `--restart <plugin>`.
  - After the land, run the live check on mizar and on one other node, and report it in one line.
