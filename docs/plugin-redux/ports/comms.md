# Comms group port plans

## Purpose & Contents

This file holds the rules every port of the comms group follows (task CWPYAtyb): social, channel (absorbing relay and tether), browser and rlm-browser. Tony approved them on 2026-11-06. Each plugin's plan is in its own file: `shared-crates.md`, `browser.md`, `rlm-browser.md`, `channel.md` and `social.md`.

Order: shared crates, browser, rlm-browser, channel, social. social lands with its seats paused, and the seats are turned on as a separate step.

## Done when

- Each plugin is `core/plugins/<name>/` with `awm-plugin.toml`, crate `awm-plugin-<name>`, `state.toml`, a one-line `run.sh` (`exec "$AWM_DIR/bin/<name>" "$@"`), and its pin `scripts/plugins/<name>.pin` listed in `deploy.toml`, all in the adding commit.
- The Python folders of social (except `mira/`), channel, relay, browser, rlm-browser, tether and kept are gone, and no Python imports `awm.kept` or `awm.tether`.
- `describe` matches the golden fixture from the live Python plugin, except for the listed surface changes.
- Every DESIGN.md seat transition, a start that never comes up, a failed push and a social restart are tested, and no message is lost or delivered twice.
- deneb's relay rooms end at the cutover, and each holder gets the room-ended notice.
- **Pre-redux state:**
  - Before each Python-deleting commit, `git -C projects/awm/.bare log 49232bf9..release -- <folder>` (mizar's release, not this worktree's stale origin) shows nothing unported.
  - `awm-statecheck check` passes after each first Rust start.
  - No migration drops a table or a column.
  - No start rewrites `social.toml`, `browser.json` or `edges.yml`.
  - The cutover kills no process it did not start, except the operator daemon and deneb's tether-relay (`channel.md`) and the Python-era browser seat containers (`browser.md`).
  - Nothing on the untouched list below changes.
- **No leaks:** no fixture, test or log holds a token, message text, name or real chat id. State files keep the modes they have, such as alcor's 0770 `services/` dirs. Secrets stay where they live: the plugin's `needs.secrets`, the unit's credential, and the `social.toml` `*_file` secrets under `/var/lib/awm/social/secrets`.
- A refresh with no comms binary change restarts no comms plugin.

## Rules for every port

- Build and test only through `scripts/rust-build.sh`, with targets in `$HOME/.cache/awm-target/port-comms`.
- Merge `feat/plugin-redux` from mizar's bare repo, never rebase. Use WIP commits, never `git stash`.
- On ENOSPC, stop and delete nothing.
- Capture each tool's `describe` before its port, as `core/plugins/<name>/tests/golden/describe.json`. A parity test diffs the Rust manifest against it.
- Inventory each node's state before the first land with `awm-statecheck inventory`. The first Rust start snapshots it to `<dir>/.pre-rust/`.
- Leave this state untouched, and list it in the inventory:
  - rlm-browser's DB and profiles on altair and capella;
  - the old `social.db` on altair, capella, lyra and shogun, and capella's `social.toml`;
  - the old relay `channels.db` on lyra and shogun;
  - mizar's `services/channel/tether/`;
  - the awm env's editable installs and `.runtime-env` files;
  - `awm/services/social/mira/`, which runs alcor's sites daemon.
- **Rollback:** revert the code and keep the live files when the format did not change. Run `awm-statecheck restore` only when a check proves damage. A restore puts back the pre-Rust state and drops what arrived during the Rust window, such as new `social_history` rows. That loss is what the guard exists to prevent.
