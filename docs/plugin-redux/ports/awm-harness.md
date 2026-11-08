# Port plan: awm-harness

## Purpose & Contents

This file holds the approved port plan for the shared library awm-harness, from the harness group's plan (task CWPYAtyb). Tony approved it on 2026-11-06. It is the plan as approved: later changes go in the port commits and the journal. The rules every harness port follows, and the state each one carries over, are in `harness-state.md`. The describe each port is diffed against is in `baseline/`.

## Plan

**Replaces.** `awm/service_components/claudedaemon/` (2.2k lines). It also replaces the harness@1 plumbing that cch and dsh each copy: the target parsing, the `harness_event` client and the `/proc` readers.

**The reversed import.** No code imports cch. But `sessionmode` defines cch's lineage paths, its mode vocabulary and the fingerprint of cch's launch flags, and cch imports them back. The gateway now asks reflection for a session's mode, so cch is the only reader left. That half moves into the cch crate with its 77 tests. awm-harness keeps only what Claude Code owns, and reads no `AWM_*` variable. A layering test enforces this.

**API.**
- `contract` (always built, harness-neutral):
  - target parsing and the target params;
  - the harness@1 result types, with a flattened `extra` map;
  - the `refusal` and `unsupported` shapes;
  - `HarnessEvent`, with the exact keys of reflection's `harness_event`;
  - a `Reporter` that spools a failed event to `unsent.jsonl` and resends it at start.
- `claude` (the default feature):
  - `ClaudeHome`, one path resolver that honours `CLAUDE_CONFIG_DIR`;
  - the roster, job and session records, and `live_session_ids`, which refuses rather than shortening;
  - `launch`, `trust`, `pins`, `daemonlog` and `cli`;
  - the async PTY protocol and `job`;
  - Claude process detection;
  - `transcript::tail`, moved from `kept`.

dsh and reflection use `contract` only. The generic procfs readers go into the SDK's `proc` module. The R9 id code moves to `awm-proto` with an SDK re-export, so knowledge and harness share it.

**State.** It reads Claude Code's files in place. It writes only a trust grant to `~/.claude.json` and `pins.json`, under the CLI's lock directory. serde_json `preserve_order` turns on in `[workspace.dependencies]`, so rewrites keep key order. This commit passes `scripts/rust-build.sh test --workspace`, and a test that relied on sorted output gets fixed.

**Tests.** Port the claudedaemon tests with fixtures synthesized from the record shapes. Add tests for the contract types, the spool and the layering rule. For parity, run the Rust and Python readers over a scratch snapshot of mizar's records and diff them. The snapshot is never committed.

**Landing.** First, right after the SDK. `preserve_order` changes every Rust binary once, so this land restarts each one once. The Python package goes in cch's commit.

Gotchas: no blocking sleep in async code. The trust tests need `git` in the build container.
