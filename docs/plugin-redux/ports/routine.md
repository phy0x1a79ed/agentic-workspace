# Port plan: routine

## Purpose & Contents

This file holds the approved port plan for routine, from task T6 of the ops group's plan (task CWPYAtyb), revised and approved on 2026-11-06. It names only what changes. The Python code and its manifests are the reference for everything the port keeps. The rules that hold for every ops port are in [README.md](README.md). Later changes go in the port commits and the journal.

## Port routine and fold events into it

Changes:
1. The `events` tool goes, with its topics and funnel. Nothing calls it or subscribes to it.
2. The `deploy` stub verb goes.
3. A run's `caller` records `as_`, else the session id, else the pid.
4. Shutdown kills each live step's process group and fails the run.
5. `schedule` gains `jitter_s` (`RandomizedDelaySec`, default 30) and `catchup_s`. With `catchup_s`, the unit gets `Persistent=true`. A fire later than `catchup_s` after its slot records the run state `skipped`. The new table `timer_slots` holds the slots.

The land queue becomes the `routine land` subcommand. `scripts/land.sh` execs it with the same flags, JSON output and exit codes 0, 1 and 3. The land state stays byte-compatible, so a Python `wait` can poll during the first Rust batch.

Dependencies:
- `network = ["*.globus.org"]`, for dvc's `run-job` step, which inherits routine's egress. routine never declares `*`.
- No needs. A failed run falls back from the fixer to the notice, then to the log.

Plain calls: `reflection start_other` and `social send`.

Consolidation: routine owns *when* (the timer, the run record and the notice). dvc owns *what* (its `run-job` step).

Tests:
- Port the 47 Python tests.
- Cross-load land state, Python to Rust and back.
- Unit-test the shutdown kill, `jitter_s` and `catchup_s`.
- Live check on mizar and altair:
  - a `describe` diff;
  - a fixture routine through every verb, with its unit files diffed;
  - a failing routine whose notice arrives;
  - `land.sh status`;
  - a no-op batch.

Landing:
- Land alone in its batch, with `--restart routine,events`, the binary pin, and the ci/land and ci/merge skill edits that run `$AWM_WORKSPACE/scripts/land.sh`.
- Fallback, until PLAN.md T8 removes the env: run Python `land.py` from a worktree pinned before the port.

Rollback: revert, and keep `routine.db` and the land dir. A restore of the land dir would revive old leases. It is allowed only when `state check` proves damage, and only for the tickets and batches, never `lease.json` or `lock`.

Gotchas:
- The batch that lands this port still runs Python. The next batch needs the Rust binary already placed on mizar.
- A branch cut before the port carries the old `land.sh`. The skill edit covers it.

