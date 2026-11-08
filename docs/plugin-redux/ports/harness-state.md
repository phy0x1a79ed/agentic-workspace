# Harness group: state safety and the state ledger

## Purpose & Contents

This file holds the rules every harness port (cch, reflection, dsh, dev and awm-harness) follows so no pre-redux state leaks or regresses, and the ledger of each piece of state those plugins own and its fate. `harness-inventory.md` records each node's state before the first land. Tony approved both on 2026-11-06.

## Rules

1. Before deleting a Python folder, run `git log 49232bf9..release -- <folder>` against mizar's bare repo (`projects/awm/.bare`) or `github/release`, never `origin` (altair, stale). Carry every listed commit into the port.
2. Before the first land, inventory each node's ledger paths in `harness-inventory.md`: mode, owner, size and record count, never contents.
3. Make the `.pre-rust/` copies with ops' `awm-statecheck`. Each port commits a `state.toml` in ops' shape. The SDK calls `snapshot_once` before `on_start`, so no port writes its own copy code. statecheck makes each `.pre-rust/` 0700, keeps file modes, never copies `.pre-rust/` into itself, and tags it with `CACHEDIR.TAG`, so the dvc mirror, which otherwise copies all of `.awm/` off-site, skips it. The secret-bearing files are declared so they get copied:
   - cch: `tokens.json` and `isolated/*/claude/.credentials.json` sit under its dirs. `~/.claude.json` and `~/.claude/settings.json` go in `[[extra]]`.
   - dsh: `home/.credentials.yaml` and `home/settings.yaml` sit under its default dir.
4. Keep migrations additive. The one edit to a file the user owns is the hooks reconcile in `~/.claude/settings.json`. It touches only the four awm hook entries and runs after the copy (approved by plugin-redux/interview, named here for Tony).
5. Fixtures and logs hold no real ids, names, tokens or message text. The captured Python fixtures get rewritten with synthetic data.
6. The rollback for every land is to revert the code and keep the live files, because no format changes. Run statecheck's restore only when a check proves damage. A routine restore would move aside the promises, tickets, trust and retired records written while Rust ran. The restore moves the live state aside and deletes nothing. Claude Code rewrites `~/.claude.json` and `settings.json`, so those are never restored whole. The rollback reverts only what cch changed there: the trust keys it added, its hook entries and its own pins.

## Ledger

"In place" means same path, same format.

| State | Fate | Removed when |
|---|---|---|
| reflection's `trust.json`, `waits.json`, `retired.json`, `tickets/`, `events.jsonl`, `lost.jsonl` | in place | never |
| `.awm/services/reflection/pending/` (cch's promises) and `.awm/services/keep/hold.json` | cch copies them into `.awm/services/cch/` and drops a `MIGRATED` marker file in each source, the one marker form | 7 days after the migration |
| dsh's `home/` (`settings.yaml`, `.credentials.yaml`, `sessions/`, `storages/`, `profiles/`, `.agent-presets`), altair's `home.bak-20260822-pre-0.1.1`, `node-bin` and `dsh.log` | in place | never |
| `~/.claude/cx/` (with `hits.log`, `locks/` and `nap.fifo`), cch's locks, `~/.claude/awm-reflection-mode.log`, `~/.local/state/awm/`, and `.awm/services/cch/isolated/` | cch's, in place, same keys, same lock kinds. `locks/` and `nap.fifo` are `[[transient]]` | never |
| Claude Code's `~/.claude/{sessions,daemon,jobs,projects}` and `~/.claude.json` | in place, writes keep key order | never |
| the four awm hook `.py` files in `~/.claude/hooks/` | replaced by `cch hook <name>` | by cch, on its first start in the release after its land, once no settings entry names them. The other files there are never touched |
| the `awm-isolated-login-sync` user unit | disabled at cch start | together with its unit file |
| untracked `.runtime-env`, `*.egg-info/` and the old `pages/dsh/dist/` | dead after the land | deleted in each land's live check |
| dsh's `.certs/` and `.sans` | handed to `auth`, with `.certs/key.pem` kept at 0600 | after `auth` serves dsh's front |
| old dev sandbox state in each worktree | re-seeded at the new path | by the worktree's owner, and dev's check lists them |
| the `dsh` conda env (Node 24) | read through `node-bin` | with the fork-build decision (§ Still unsure) |
| `enabled.json` key `cx` (true on mizar, alcor, capella and altair) | read as the fallback when `AWM_CX_POOL` is unset, so the pool stays on | with interview's T8 |
| `~/.claude/backups/settings.json.*` that cch writes | the reconcile's backups. The folder is Claude Code's | cch keeps the newest 5 of its own and touches nothing else there |
| `.pre-rust/` and `.rust-<ts>/` copies | the rollback copies | a listed step a week after the last land passes its checks, run only with Tony's OK at that time |
