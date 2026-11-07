# The `cch` plugin

## Purpose & Contents

This file holds what a reader cannot recover from the code about the `cch` plugin: what it serves, which state it reads and writes in place, and how to check a node. The harness contract itself lives in `docs/redux/drafts/harness-contract.md`.

The plugin is one static binary, `cch`, which the deploy places at `<AWM_DIR>/bin/cch`. It needs no install step. It serves tool `harness-cc` (harness@1 over Claude Code sessions), which is off MCP. reflection is its main caller.

## State

cch keeps no database. It reads Claude Code's own files in place under `$CLAUDE_CONFIG_DIR`, else `~/.claude`: the session records, the daemon roster, the job records and the transcripts. It writes only these:

| Path | What |
|---|---|
| `<AWM_DIR>/services/reflection/pending/` | the promise store: one record per session that owes a follow-up, and `unsent.jsonl`, the events reflection did not take |
| `${AWM_CX_STATE:-~/.claude/cx}/` | lineage records under `starts/`, the warm pool's state and the reboot mark |
| `~/.claude.json` | trust for an awm worktree a `set cwd` moves into |
| `~/.claude/settings.json` | restored after a `/model` or `/effort` rewrites the user defaults |

**CAUTION** The promise store still sits in reflection's old folder. Part 2 of the port moves it to `<AWM_DIR>/services/cch/`.

`state.toml` names what `awm-statecheck` copies before the first Rust start.

## Part 1

Part 1 of the port serves the addressing, typing and promise verbs. Every part-2 verb, and the isolated and kept-stop branches, answers `ok: false` with `code: "not_yet_ported"`. `list` is a part-2 verb, so `probe` reports a node as not answering until part 2 lands.

## Verify

1. Run `awm harness-cc platform`. It lists 17 capabilities.
2. Run `awm harness-cc pending`. It lists the follow-ups cch owes, the ones the previous process owed included.
3. Run `awm harness-cc live_session_ids`. `ok: false` names the record it could not read.
4. Run `awm harness-cc resolve --pid <a session's pid>`. Check `mine`, `hosting` and `mode`.

## Environment

| Var | Default | Effect |
|---|---|---|
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude Code's home |
| `AWM_CX_STATE` | `<Claude home>/cx` | lineage records and pool state |
| `AWM_CX_CLAUDE` | the `claude` on PATH | the CLI `stop` runs `claude stop` and `claude rm` with |
| `AWM_CX_TRUST_FILE` | `~/.claude.json` | the trust map `set cwd` reads and writes |
| `AWM_CX_PROJECTS` | `<workspace>/projects` | where an awm worktree that `set cwd` may trust lives |
