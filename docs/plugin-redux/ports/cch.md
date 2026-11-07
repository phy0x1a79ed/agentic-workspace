# Port plan: cch

## Purpose & Contents

This file holds the approved port plan for cch, in two parts that land as one commit, from the harness group's plan (task CWPYAtyb). Tony approved it on 2026-11-06. It is the plan as approved: later changes go in the port commits and the journal. The rules every harness port follows, and the state each one carries over, are in `harness-state.md`. The describe each port is diffed against is in `baseline/`.

## Plan

### Port cch, part 1: records, addressing, typing, transcript and promises

cch is 11.8k lines and lands as one commit in T10. Part 1 is committed to the branch and never submitted to the land queue.

**Surface.** `harness-cc` keeps its 29 verbs and their timeouts. Listed changes:
- `start` drops its duplicate `task_id`, `role` and `suggest` params;
- `list` rows gain `session_id`, `parent`, `alive` and `transcript`;
- a send to a missing job answers `code: "gone"`.

The contract draft gets amended to the served behaviour in T12: the `stop` default, the `status` fields and `compact` with a ref.

**Design.**
- One crate holds the records, a `Lane` trait over tmux and the daemon PTY, the sender, the promises, the transcript tail, the watcher, the control verbs, the screen walks, the guards, the settings guard, the lineage with `mode_of`, and one verb table.
- One tokio runtime runs it all, with today's timing budgets.
- Each external effect sits behind a trait, so the Python tests port one to one.

**Invariants.** Each comes from the Python, and each has a test:
- typed is not delivered;
- a "could not tell" never licenses a second delivery;
- a promise is written before its watcher starts;
- never retry after Enter;
- never key on transcript line order;
- never guess a session;
- screen walks on one session never overlap;
- an unreadable record means live;
- the settings guard keeps the file's formatting.

**State.** Claude Code's files and `~/.claude/cx/`, in place. The promise store moves in T10.

**Crates.** `regex`, with two patterns written by hand, and `sha2`.

**Tests.** Port the 389 part-1 tests, transcript tests first. The transcript and `box` fixtures get synthesized from the record shapes. Nothing is copied from a node.

Gotchas: every path goes through `ClaudeHome`.

### Port cch, part 2: lifecycle, keeper, isolated sessions and hooks, then land it

**Replaces.** `lifecycle`, `daemon`, `isolated`, the warm pool, `memwatch`, the `keep` and `cx` adapters, `hooks/`, `bin/isolated-*` and the login-sync unit.

**Needs and calls.** None. Its calls each degrade when they fail:
- `scope create|tag`;
- `operations task_attach`;
- `social send`;
- `reflection message|harness_event`.

`daemon_ensure` runs in-process.

**Migration.** On first start, before it answers any verb, cch copies reflection's `pending/` and `keep/hold.json` into `.awm/services/cch/` and drops a `MIGRATED` marker in each source. The rollback copies `services/cch/pending/` back into reflection's `pending/` and removes the marker, so reflection's sweep never declares a live promise lost.

**Hooks.**
- The four hooks become `cch hook {brief,plan-mode,name,subagent-cap}`, run from `$AWM_DIR/bin/cch`. Each exits 0 on every path, keeps today's caller-pid behaviour, and reads `AWM_HUB_URL`, else `127.0.0.1:7819`.
- At start, cch replaces only the four awm entries in `settings.json`, never adding beside an old one, and keeps a backup.
- `AWM_CCH_HOOKS=off` turns the reconcile off in the sandbox.
- Behaviour change: hook entries appear where none exist today. alcor, capella and altair gain brief and subagent-cap, and shogun and lyra gain all four.
- If operations already moved its hook, this land deletes `awm/plugins/operations/hooks` and its `deploy-hooks.sh` rows.

**No package manager.**
- `install.sh` goes.
- `bin/cx` stays as bash.
- The isolated image is built from an embedded Dockerfile.
- The login sync becomes a background task.
- The daemon unit's `AWM_CONDA_ROOT` comes from the install config `[env]`, else from the running unit's environment, never from a hard-coded default. No node sets it today, and the unit is transient, so a reboot loses it. Before the land, set `[env] AWM_CONDA_ROOT` in each node's `config.toml` through the cloud `awm_config` role: `~/miniforge3` on mizar and alcor, `~/lib/miniforge3` on altair, capella and lyra, and shogun after a check.

**Consolidations.**
- One verb table replaces the legacy adapters.
- The keeper runs in-process.
- One paths module.
- `trust` merges into awm-harness.
- The `observation` seam goes.
- `pool_wanted` reads `AWM_CX_POOL`, else `enabled.json` key `cx`.
- The dead code goes.

**Carried invariants.**
- The peer privacy filter, today only in `cx_adapter`, applies the SDK's `visible_rows` and `transcript_entered` to the session, pool and isolated rows of `list`. Python appends the isolated rows after the filter, which is the bug to avoid. Peer replies leave out the new `transcript` field.
- `caller_refusal` and `station_refusal` refuse as today.

**Fixes.**
- An isolated start with no scope is refused.
- `_ensure_lineage` writes `mode: "worker"`.
- A container login copy holds only `LOGIN_KEYS`, never `refreshToken`. It is written 0600 through a temp file, and deleted when the host is signed out or its login has expired. Each of the three has a test.

**Tests.** Port the part-2 tests and the hook tests, which run the binary against a fake gateway. Add tests for the reconcile and the migration.

**Parity.** Run the Rust cch in the sandbox against the live `~/.claude`, with hooks, revive, keeper launch and sync off. Compare the read-only verbs and `mode_of` for every live pid.

**Cutover.**
- With a user systemd manager, a restart kills no session. The daemon and each launch run in their own units, and every record is on disk.
- On a node with no user manager, list the sessions that Python cch launched with `ps -o pid,pgid,sid`. Land there only when none shares cch's process group. Rust launches such sessions under `setsid`.
- Land in a quiet window: `land.sh submit --restart cch`.

**Live check.** On mizar, then alcor, then altair, shogun and lyra when they answer:
- `ready`, `whoami` and `list_other` with every pre-land session;
- `pending` shows the migrated promises;
- the four `cch hook` entries are in `settings.json`;
- a throwaway session's `compact` logs `done`;
- the mirror dry run excludes `.pre-rust`;
- no Python copy runs.

Gotchas: the settings guard and the reconcile share one mutex. `build_flags` and `mode_of` change together.
