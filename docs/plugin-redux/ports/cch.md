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
- The daemon unit's `PATH` gains the directories in `[env] AWM_SESSION_PATH`, a colon list, ahead of the system ones. With no knob, the keeper takes them from the running unit's `PATH`. A typical install sets nothing, and its sessions get the plain `PATH`. cch sets no conda variable. The nodes that keep miniforge get the knob through homelab_base's miniforge block, in plugin-redux's T8.

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

**Session events** (trilium-design `CONTRACT.md` § port-harness, item 1). A new `watch` module reports each session's lifecycle to reflection `harness_event` as it happens, with no timer:
- inotify on `~/.claude/sessions/` and the jobs dir. A new session file is `started`. A changed `name` is `renamed`. A changed bridge id is `remote_control`, with the new `rc_url`, null when the bridge is off.
- One pidfd per live session pid. The pid's exit is `stopped`, a crash or a `kill -9` included. A session id that comes back under a new pid within 3 s is neither stopped nor started, because the daemon can swap the worker mid-command.
- `docker events` filtered to cch's containers gives `started` and `stopped` for isolated sessions.
- At boot the watcher reads its state from disk and sends nothing.
- cch's own `start`, `stop` and `set name` reach reflection through the same watcher, so each event has one source. `task_id` comes from the lineage record.
- `list` rows gain `task_id` from the lineage record. reflection names the existing `link` as `rc_url`.

**Remote Control at turn end** (item 3). `remote_control` on another session that is `busy` takes the path a self call takes today. cch types the command, which queues behind the turn, and answers at once with `{applies: "turn_end", link: null}`. It does not wait out the link timeout. The link arrives as the `remote_control` event when the command runs. `remote_control` off on a busy session reuses the promise that runs the self path's disconnect modal at turn end.

**Sender prefix on cch's own notices** (the reflection sender-names rule). Each line cch types into a session on its own account starts with `from cch@<node>: `. That covers the lost follow-up notice, the resume prompt after Remote Control turns on or off, and every other notice in `promises.rs`, `resume.rs` and `control.rs`. A follow-up that reflection passed in already carries its prefix, so cch types it as given. Test each notice for the prefix.

**Tests.** Port the part-2 tests and the hook tests, which run the binary against a fake gateway. Add tests for the reconcile and the migration. Add watcher tests on a temp sessions dir: a create, a rename, a bridge change, a pid exit, a worker swap inside the grace window, and silence at boot. Add a test of `remote_control` on a busy fake target.

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

Gotchas: the settings guard and the reconcile share one mutex. `build_flags` and `mode_of` change together. cch lands before the Rust reflection. Python reflection's `harness_event` writes any kind other than `clear` into its ledger, so the watcher sends only while reflection's describe lists `harness_event`'s `session_kind` param. While the gate is closed it reads the describe again before each event, and it reads it again after any failed send. The watcher must not hold a pidfd on a pid it does not own.
