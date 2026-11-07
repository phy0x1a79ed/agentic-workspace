# Plugin redux — interview stack

Goal: settle what the awm plugin redux covers and how plugins install and run now that the core is Rust, then submit a formal plan. Prompt (2026-11-06): "we moved to rust... why are we even doing pip installs?"

Decisions: `search` for journals tagged `decision` in awm/plugin-redux.

## Current

- `build` — executing docs/plugin-redux/PLAN.md. T1 landed on release (49232bf9; rollout stuck at lyra). T2's three parts are committed on feat/plugin-redux and unpushed (see `t2-finish`). T3 is done. T4 is under way: each worker's plan is checked here, then submitted for Tony's approval.

## Waiting on the user

- disk: mizar / is at 100% (548 MB free on 2026-11-07). Asked Tony to delete the core/target folders of idle scopes (~60 GB) and the docker build cache plus dangling images (keep awm-rust-build:1.99.1). A disk-full write truncated this file once.
- lyra: land batch 9a59ed8ad3de (ticket b3b597e326e0) is stuck at lyra, which has no edges.yml and was never provisioned. Choose (a) provision it with cloud awm_config and edges, or (b) shut lyra's WSL so the rollout skips it as an asleep sleeper. request_human is filed and the lease is held. deneb, shogun and altair still run the old release.
- Plan questions for Tony at each approval, in the plan's "Still unsure about": comms D2 (the mira daemon) and D3 (rlm-browser); harness Q1 (the dsh fork build); knowledge (search off LanceDB, the model artifact, kb's Python child and kb's tool vs R134, the journal embeddings tables, transcripts' sweep); ops (deneb as an operations caller in edges.yml).

## Preferences

- 2026-11-06: four workers, balanced by size, not one per plugin. Core plugins may share crates.
- 2026-11-06: planners are Opus high, and they execute after planning. This session merges.
- 2026-11-06: run the walk with dfs-interview. "when we have things scoped out, formally submit a plan."
- 2026-11-07: planners run in plan mode. Correct a running worker by message, never by restart (memory planners-plan-mode-nudge).
- 2026-11-07: T4 revised. Each worker sends its plan here, this session checks it alone and says "submit", the worker calls ExitPlanMode, and Tony approves each plan individually. No merge gate across workers.
- 2026-11-07: T3 waited for seats-pause's message before starting.

## Stack

- `build` — the approved plan, T1–T10. The todo list mirrors it.
- `t2-finish` — T2 commits on feat/plugin-redux: pins 891e8692, gateway 0e423392, SDK 29900ddb. Before T2 lands:
  - delete test/contract/test_plugin_calls.py and awm/services/search/tests/test_needs.py (they test the removed guard), and fix the guard mentions in the INSTALL.md of rlm-factorio and search;
  - make a [[needs.contract]] with no provider refuse (R117 item 8), and fix contract § Contracts item 6 and its CAUTION;
  - contract doc: the core roots include core/plugins; the install stamp skips UNSTAMPED; the plugin state dir is <AWM_DIR>/plugins/<plugin>/;
  - the core version covers awm-cli's dependency closure, not core/ minus plugins;
  - add plugins/* to core/Cargo.toml members, and --locked to release-core.sh;
  - page pipeline: core/plugins in awm/scripts/build.sh, scripts/release-pages.sh and awm/tsconfig.pages.json; pins.sh pages_version hashes core/plugins/*/pages/*;
  - root plugins' [[service.archive]] readable whether loaded or not (search kinds());
  - the gateway's call frame carries `plugin` (SDK Call.plugin);
  - the SDK's privacy module re-exports awm_trust::privacy once ops moves it there;
  - awm-trust's door far end answers `not installed`, not "Unknown tool".
  The isolated-door e2e test flaked once (it listed only describe).
- `cross-group` — settled so far:
  - social calls only reflection. reflection gains probe, the `gone` code, transcript paths, include_stopped and typed start codes. list_other answers ok:true with errors[].
  - Transcripts are read on the seat's node.
  - transcript_tail goes into awm-harness for cch only.
  - preserve_order is on for the whole workspace (harness's awm-harness land restarts every Rust plugin once).
  - R9 ids move to awm-proto.
  - privacy moves to awm_trust::privacy.
  - One generic cloud-role drop-in for external units (alcor operations/social/browser, deneb relay, awm-journal on mizar, alcor and capella) lands right after awm-trust.
  - awm-store lands first, with ops' extras.
  - The operations hook stays until cch replaces it; whichever lands second deletes it.
  - dsh's front is [[service.front]]: it reuses the edge leaf and is landed by harness.
  - claude-science declares [[service.front]].
  - The T11 operations cutover holds the batch before alcor.
  - R135 item 6 needs amending: social calls reflection only.
- `seat-redesign` — seats/pause handed social's seat redesign (items 1–8) to the comms port. Settled by Tony: #bots gets no seat; Hallam Hive #general is cooperative on altair; seats stay paused over the weekend, with the Python social question reopened Monday 2026-11-09. Asked seats/pause S1–S5 (brief root, values, outside-lab test, down-notice target, crash rule): S1 and S4 are pending. Answers are in ~/.cache/plugin-redux-briefs/seat-side-answers.md. Not ours: agentic-commons, the brief texts, /new-agent, the RC toggle.
- `live-bugs` — from the knowledge plan:
  - the scatter-gather skill calls gather/scatter, but the verbs are git_gather/git_scatter;
  - goal-alignment calls removed journal verbs;
  - debrief passes kind:"journal", which the gateway refuses;
  - mizar and alcor hold an agent-comms tag under different ids. Rename one before journal lands, or the first sync blocks journal reads between them.
- `hot-reload-followups` — --force and adopted env are done in 0e423392, with tests. Open: deneb relay linger; hold-mode blocked card unverified.
- `refresh-handoff` — deterministic-refresh landed in 5bbbd3ae. lyra (2026-11-07): `awm` remote set to the GitHub backup (lyra's key is not on mizar), socat installed, and a hand-written config.toml (backup config.toml.bak-20261107). Still open: mizar-push, and the stock deploy.toml `[refresh] kind="none"` fix.

## Workers

Briefs: ~/.cache/plugin-redux-briefs/full-<group>.md. All Opus high, task CWPYAtyb, in plan mode.
- comms — job e44d901a, plugin-redux/port-comms, https://claude.ai/code/session_012TZ1zwTFfh7Txjdm2h5Rma. Plan /home/tony/.claude/plans/kind-stargazing-falcon.md: checked, told to submit.
- harness — job eecc2448, plugin-redux/port-harness, https://claude.ai/code/session_012TZ1zx4PnARsz9Am5dpp85. Plan /home/tony/.claude/plans/sparkling-sniffing-sonnet.md: checked, told to submit.
- knowledge — job 2a70a96d, plugin-redux/port-knowledge, https://claude.ai/code/session_012TZ1zwibR5V8A9BD4Hq2tt. Plan /home/tony/.claude/plans/hidden-rolling-bentley.md: fix list sent.
- ops — job 8efddb16, plugin-redux/port-ops, https://claude.ai/code/session_012TZ1zxbfDVLySsTQCPp1rq. Plan /home/tony/.claude/plans/abundant-singing-moon.md: fix list sent.

## Closed

- `core-set` — journal 2a343d6c (core set, consolidations, wake not core, kb its own source plugin, reserved names).
- `deps-semantics` — journal a5f837ac: a need is a must; calls need no declaration; no optional flag.
- `board-store` — journal fc14982b: operations tracks the queue state; trilium exposes and manages it.
- `plan` — journal 5d8bd9db: four Opus-high workers by concern (~17k lines each), shared crates allowed in core; PLAN.md.
- `rust-port` — journal 1c0f4acc: core plugins port to Rust; any language for others; consolidate; per-plugin planners.
