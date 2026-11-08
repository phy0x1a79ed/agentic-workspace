# Port plan: dev

## Purpose & Contents

This file holds the approved port plan for dev, from the harness group's plan (task CWPYAtyb). Tony approved it on 2026-11-06. It is the plan as approved: later changes go in the port commits and the journal. The rules every harness port follows, and the state each one carries over, are in `harness-state.md`. The describe each port is diffed against is in `baseline/`.

## Plan

**Surface.** Tool `dev` keeps its five verbs (`start`, `stop`, `restart`, `seed`, `status`), each taking a required `worktree`, with a 120 s timeout. Its describe is unchanged. It has no state and no needs.

**Changes.**
- `seed.py` becomes `dev sandbox seed`.
- The `python3` one-liner in `run.sh` becomes `awm services` piped through `jq`.
- The sandbox script moves to `core/plugins/dev/sandbox/run.sh`.
- The sandbox runs the worktree's own Rust plugins. It adds `core/plugins` to `core_roots` and links each built binary into `sandbox/.awm/bin/`. T4, T6 and T10 run their parity checks there.

**Tests.** Port the Python tests. Test `sandbox seed` against a fake `/invoke`. For parity, diff describe and the refusal texts.

**Landing.** First plugin, because it is the smallest, so it proves the pin and land flow. Check `dev status` on mizar and capella. Stop old-path sandboxes before their worktrees merge.

Gotchas: `run.sh` keeps its Python-env helpers until the last Python plugin leaves the tree.
