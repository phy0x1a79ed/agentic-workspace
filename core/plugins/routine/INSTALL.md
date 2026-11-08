# The `routine` plugin

## Purpose & Contents

This file covers what the `routine` binary runs, the state it keeps, and how to check it on a node. The plugin is one static binary, `routine`, which the deploy places at `<AWM_DIR>/bin/routine`. It needs no install step. The binary serves three things: the `routine` service, the land queue (`routine land`, which `scripts/land.sh` runs), and the pre-redux state check (`routine state`).

## State

| Path | Holds |
|---|---|
| `<AWM_DIR>/services/routine/routine.db` | installed routines, their runs and their timer slots |
| `<AWM_DIR>/land/<project>/` | the land queue: tickets, the open batch, the lease, logs and settled batches |
| `<unit dir>/awm-routine-*.{timer,service}` | one systemd user timer per scheduled routine |

The unit dir is `AWM_ROUTINE_UNIT_DIR`, else `$XDG_CONFIG_HOME/systemd/user`. `state.toml` declares this state for `routine state`.

**CAUTION** The land state is the format `land.py` wrote, byte for byte. A Python `land.sh` on an old branch and the Rust one read each other's tickets, batches and lease. Keep the JSON writer in `src/land/json.rs` matching Python's `json.dumps(indent=1)`.

**CAUTION** The first run of the service or of `routine land` copies the state once into `.pre-rust/` before it writes anything. A failed copy refuses the start. Never delete `.pre-rust/` by hand: run `routine state prune` with Tony's dated OK.

## Timers

A timer only fires: its unit runs `awm routine trigger --trigger timer`. Every timer is `Persistent`, so a slot missed while the node was down fires at the next boot. With `catchup_s`, a fire more than `catchup_s` after its slot, or a second fire for a slot that already ran, records a `skipped` run. The slot check asks `systemd-analyze calendar --base-time`. Where systemd cannot answer, the fire runs.

## Verify

1. Run `awm routine list`. It lists the routines installed on this node.
2. Run `scripts/land.sh status` with `AWM_WORKSPACE` set to the workspace that deploys awm. It prints the merger, the lease, the batch and the queue as JSON.
3. Run `<AWM_DIR>/bin/routine state check`. It exits 0 when no pre-redux state was lost.
