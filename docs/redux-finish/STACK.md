# Redux finish — stack

Goal: bring mizar and alcor to the post-redux state in this one session, from the work the plugin-redux workers left. Other nodes wait. Prompt (2026-11-07): "can we finish the work here, in 1 session? … Lets get mizar (and alcor since it holds crucial services) to the post redux state before worring about other nodes."

Decisions: `DECISIONS.md` beside this file. Questions and issues live in this file's stack, under the plugin they belong to.

Sources: the worker debrief journals 389a0111 (ops), 8caec34d (knowledge), 449af06d (harness), 004f4c13 (comms), f9c57c47 (trilium), 4bbcc79a (hpc-ssh-guard), 3781f166 (coordinator), and the handoffs they name.

## Current

- Batches 1 to 3 are live on mizar and alcor. Each `core/plugins` plugin except reflection runs from `<AWM_DIR>/bin/<plugin>`, and auth alone holds the TLS ports.
- `feat/port-harness` holds the Rust reflection, session events, the order puller, the non-core runtimes and the clean-install script. It is unmerged, and journal ace5dab4 lists its landing steps.
- Cloud `feat/redux-batch3` f6d8279 is unmerged, because cloud `dev` is checked out in `redux-dev-apply`. Until it lands, the next `--tags awm_config` apply brings back `board` and alcor's old env.
- A refresh restarts every plugin whose binary changed, not only `AWM_REFRESH_RESTART`. A lockfile change restarts only the plugins whose locked dependencies changed.

## Waiting on the user

- When you are at a device: one passkey sign-in on a node edge, a visit to a node's portal on your phone, the claude-science browser sign-in on alcor, and a dashboard render. None gates the work. The Rust edge offers only HTTP/1.1, so an HTTP/2-only client fails.
- Decide cch's warm pool. A seed session dies every 50 s on mizar and alcor, because Claude Code will not trust the home directory without a prompt. The options are in the batch 3 journal.

## Preferences

- 2026-11-07: one session does the work. Sonnet-medium subagents take well-defined tasks.
- 2026-11-07: track the walk with dfs-interview. Questions and issues go on the stack; no separate issues file (Tony: "issues.md is redundant").
- 2026-11-07: do not use the board; it is being remade. reflection and journals work.
- 2026-11-07: mizar and alcor first. Other nodes later.
- 2026-11-07: start from the original interview's decisions (DECISIONS.md § inherited). Work plugin by plugin. Related issues are prioritized case by case.
- 2026-11-07: work in the old worker groups, one batch at a time: survey, plan the completion, implement, then return to this stack. Tony: "we can use the same groups as before rather than 1 plugin at a time. for each batch, we will survey and plan out the completion of the redux, then implement, then return to teh dfs interview stack. use stack.md for any questions to me." Groups in order: ops, comms, trilium-board + opt-in, knowledge, harness.

- 2026-11-07: remove and rewrite, not only add. Tony: "dont just add code, remove stuff that's no longer needed and rewrite, change. Adding is safe but costly in the long run for bloat. this is out chance."
- 2026-11-07: lean tests. Unit tests run in about 10 s; the full run at the end takes at most 1 min. Consolidate, curate and change tests, not only add them.
- 2026-11-07: transition states are fine. Tony: "we are going to land in plenty of transitions states throughout this work. that is ok. lets just keep our head down and work through it." The aim is to get through, then check and refine, and return to normal operations.
- 2026-11-07: trim tests first. Each batch curates its crates' tests before it builds and lands them. Tony: "the real problem is 2k tests. surely this is excessive." Keep a test that guards a wire format, stored state, a security gate or a shared golden vector. Fold the rest into table-driven tests or delete it. 10 s is a guideline, not a target.
- 2026-11-07: the stack holds large-scale items only, such as which plugin group comes next. Tony: "use the dfs-interview for large scale items like what plugin to work on. for the tests, trim them now". Smaller work is done in place, not pushed as a topic.
- 2026-11-07: deneb is out of scope. Only mizar, and alcor when needed. Stay focused: optional work waits.
- 2026-11-08: state an estimate before any step likely to pass a minute, set its timeout to about twice that, run chained steps separately, and check in when a step passes twice its estimate. Tony: "dont just accept long runtimes... Estimate how fast something should take and check in if it takes significantly longer".
- 2026-11-08: build for the end state in `DECISIONS.md`. Tony: "for comms, I'd just dev for end state rather than implementing a fallback." and "Order doesn't really matter since we aim for endstate."

## Stack

Phase 1 scopes each plugin; phase 2 does the work in this order. Each topic lists its known questions and issues.

- `scoping` — the plugin-by-plugin walk.
  - `trilium-board` — telemetry landed (acadb279); vault feat/trilium-vault 92a7d431 not submitted; cloud feat/telemetry-optin unrun; R79 (deneb's outbound calls) first. Needs the Rust operations, the vault API and reflection's `reflection/sessions`.
  - `reflection` — on `feat/port-harness`, tests pass, unmerged. dsh session events and the order-puller handover ride with it.
  - `python-out` — each non-core Python plugin has its own runtime on `feat/port-harness`. `awm/install.sh` and the shared `awm` env go at its landing.
- `later-nodes` — capella, lyra, deneb, shogun, altair; hpc-ssh-guard apply. deneb's `awm_plugins` must drop httpsfront in the same pass. Two checks wait here: dvc's mirror sending a symlink-only dir to Globus, which needs a node with `dvc.toml`, and the public profile's parity, which needs deneb.

## Workers

Read-only survey subagents (Sonnet medium), one group at a time. Brief /home/tony/.claude/jobs/b8680c94/tmp/survey/COMMON.md; reports beside it as <group>.md. Done: ops.

Test trimmers (Sonnet medium, in this worktree, no commits), brief /home/tony/.claude/jobs/b8680c94/tmp/trim/BRIEF.md, started 2026-11-07: operations; dvc + awm-dvcdata; cch; harness + gateway + cli; the other landed crates.

## Closed

- `opt-in` — dropped. Tony: "Drop opt in, do not implement."
- `social`, `browser`, `channel`, `rlm-browser`, `kb`, `scopes`, `landed`, `cross-cutting` — live on mizar and alcor.
- `front-declarations` — auth opens every `[[service.front]]`, see DECISIONS.md § batch-3.
- `verify` — run in batch 3. The two checks that cannot run here moved to `later-nodes`.

- `remote-build` — shelved (Tony: "lets shelf remote-build. we may not need it."). capella has 16 cores and 68 GB; mizar has 4.

- `operations` — live on alcor; see DECISIONS.md § ops-questions.
- `auth` — live on mizar and alcor; see DECISIONS.md § auth.
- `dvc` — merge t7 with operations; mizar live checks; altair and capella later.
- `queue-unblock` — no land queue this session; merge by hand, refresh mizar and alcor only.
- `inherited` — the original interview's decisions are the starting point.
