# The `cch` plugin

## Purpose & Contents

This file holds what a reader cannot recover from the code about the `cch` plugin: what it serves, which state it reads and writes in place, what its first start changes, and how to check a node. The harness contract itself lives in `docs/redux/drafts/harness-contract.md`. Each knob and its default is documented on its field in `src/paths.rs`.

The plugin is one static binary, `cch`, which the deploy places at `<AWM_DIR>/bin/cch`. It needs no install step. It serves tool `harness-cc` (harness@1 over Claude Code sessions), which is off MCP. reflection is its main caller. The same binary runs the four Claude Code hooks as `cch hook <name>` and the state check as `cch state <cmd>`.

## State

cch keeps no database. It reads Claude Code's own files in place under `$CLAUDE_CONFIG_DIR`, else `~/.claude`: the session records, the daemon roster, the job records and the transcripts. It writes only these:

| Path | What |
|---|---|
| `<AWM_DIR>/services/cch/pending/` | the promise store: one record per session that owes a follow-up, and `unsent.jsonl`, the events reflection did not take |
| `<AWM_DIR>/services/cch/hold.json` | the keeper's holds on the Claude Code daemon |
| `<AWM_DIR>/services/cch/isolated/` | the isolated sessions' tokens, and one Claude home per session |
| `${AWM_CX_STATE:-~/.claude/cx}/` | lineage records under `starts/`, the warm pool's state and the reboot mark |
| `~/.claude.json` | trust for an awm worktree that a session moves into |
| `~/.claude/settings.json` | the four awm hook entries, and a restore after a `/model` or `/effort` rewrites the user defaults |

`state.toml` names what `awm-statecheck` copies to `.pre-rust/` before the first Rust start.

## First start

Before cch answers any verb, it runs the first four steps in order:

1. It takes the `.pre-rust/` snapshot once. A failed snapshot stops the start, so nothing writes unsnapshotted state.
2. It copies reflection's old `pending/` and the keep service's `hold.json` into its own folder, and drops a `MIGRATED` marker in each source. The sources stay for 7 days.
3. It replays the promises it owes.
4. It replaces the four awm hook entries in `settings.json` with `cch hook <name>`, and keeps a backup as `~/.claude/backups/settings.json.<ts>.cch`. It touches no other entry. Set `AWM_CCH_HOOKS=off` to skip this step.
5. In the background, it disables the old `awm-isolated-login-sync` user unit when one exists.

**CAUTION** `awm/scripts/deploy-hooks.sh` no longer carries cch's hooks. An older copy of that script adds the Python entries back beside cch's, and every hook then fires twice.

To roll back, revert the code. Copy `services/cch/pending/` back into reflection's `pending/` and remove its `MIGRATED` marker, so reflection's sweep declares no live promise lost.

## Session events

cch reports `started`, `stopped`, `renamed` and `remote_control` to reflection `harness_event` as they happen. It sends them only while reflection's `harness_event` describe lists the param `session_kind`. Python reflection lacks that param and would write the events into its ledger. At boot cch reads the current state and sends nothing.

## Verify

1. Run `awm harness-cc platform`. It lists the optional verbs in `capabilities`.
2. Run `awm harness-cc list`. Every live session appears with `session_id`, `alive` and `transcript`.
3. Run `awm harness-cc pending`. It lists the follow-ups cch owes, the migrated ones included.
4. Run `awm harness-cc daemon_status`. The keeper reports the daemon unit.
5. Run `grep -c 'cch hook' ~/.claude/settings.json`. Each of the four hooks has an entry.
6. Run `cch state check`. It exits 0 when nothing in the snapshot is lost.
