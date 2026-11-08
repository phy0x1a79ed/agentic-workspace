# Redux finish — stack

Goal: bring mizar and alcor to the post-redux state in this one session, from the work the plugin-redux workers left. Other nodes wait. Prompt (2026-11-07): "can we finish the work here, in 1 session? … Lets get mizar (and alcor since it holds crucial services) to the post redux state before worring about other nodes."

Decisions: `DECISIONS.md` beside this file. Questions and issues live in this file's stack, under the plugin they belong to.

Sources: the worker debrief journals 389a0111 (ops), 8caec34d (knowledge), 449af06d (harness), 004f4c13 (comms), f9c57c47 (trilium), 4bbcc79a (hpc-ssh-guard), 3781f166 (coordinator), and the handoffs they name.

## Current

- Batch 2, seven packages: social, browser, channel, rlm-browser, kb, scopes, telemetry roster. Plan: `~/.claude/plans/noble-splashing-nygaard.md`. Batch 1's plan: `~/.claude/plans/redux-finish-batch1-ops.md`. T18 is now retire (Tony, 2026-11-08), so batch 3 is not planned in this session.
- Comms is live on mizar and alcor at release 8cde8e40. Checked: rooms through deneb's Python relay in both directions, delivery into a session, `channel state check`, 17 of 17 Slack seats adopted, `paused = true`, the remote-control gate, a browser seat, an outbound Slack DM. The inbound Slack path is unconfirmed until a person replies.
- The knowledge merge is on `wip/redux-knowledge` in worktree `projects/awm/redux-knowledge`, branched at d63ee920. Merge `feat/redux-finish` into it, then fast-forward. It holds the kb and scopes trims (never compiled except awm-kb-journals), 31 journal tests (green, 8.2 s), and the tag sync at start that replaced the six-hour pull. Left for T7: restore scopes' `a_peer_may_not_sweep`, clippy in `awm-embed/src/embed.rs:224`, the process-wide env race in search's `privacy` and guide's veil test.
- T10 is mostly done. Cloud `dev` has a local merge 9a3d3b9 of `feat/kb-journal-unit` in `projects/cloud/redux-dev-apply`, not pushed. The model files are in `.awm/models` on mizar and alcor, and they match the pinned hashes. **CAUTION** `--tags awm_config` also applies pending `edges.yml` changes: strict `host_keys` and deneb as an `operations` caller. Check how the edge treats `host_keys` before applying it.
- A refresh restarts every plugin whose binary changed, not only `AWM_REFRESH_RESTART`. A lockfile change therefore restarts every plugin on the node.

## Waiting on the user

- When you are at a device: one passkey sign-in on a node edge, and open a node's portal on your phone. Neither gates the work. The Rust edge offers only HTTP/1.1, so an HTTP/2-only client fails.

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
  - `opt-in` — Tony: "an opt in mechanism for prototype features like the deploy queue and the board. this way we can try features in a few select sessions rather than fleet wide". Before trilium-board, its first user.
  - `trilium-board` — telemetry landed (acadb279); vault feat/trilium-vault 92a7d431 not submitted; cloud feat/telemetry-optin unrun; R79 (deneb's outbound calls) first. Needs the Rust operations, the vault API and reflection's `reflection/sessions`.
  - `social` + `browser` — feat/port-comms + cloud feat/rust-singletons on alcor. Seats stay paused until Tony turns them on.
  - `channel` — on feat/port-comms; the relay half runs on deneb.
  - `rlm-browser` — on feat/port-comms.
  - `kb` — journal, search, transcripts folded in; feat/port-knowledge passes cargo check only, journals has no tests, clippy fails; cloud feat/kb-journal-unit; model files; the agent-comms tag clash between mizar and alcor.
  - `scopes` — feat/port-knowledge, after kb.
  - `front-declarations` — auth does not open `[[service.front]]` listeners yet, and `auth front` does not exist. Build them when dsh moves behind the edge; dsh then drops out of `EDGE_MATERIAL_PLUGINS`.
  - `reflection` — wip/port-harness-reflection compiles, tests unfinished; dsh session events ride with it; the order-puller handover with operations.
  - `python-out` — plan T8: drop the core Python install path; each non-core Python plugin gets its own env. Before removing the conda `dvc` env: the stock project's git hooks still call it (the Rust dvc rewires hooks only at provisioning). Delete alcor's untracked `awm/plugins/{operations,routine,dashboard}` leftovers. `awm/service_components/tlsfront` and `awm.config.tokens` stay until claude-science stops importing them.
  - `landed` — cch, dsh, dev, mic, fileviewer, routine, dashboard, telemetry: confirm each is healthy on mizar and alcor. Issue: statecheck cannot copy a directory `[[extra]]` (took mizar's cch down 4 minutes).
  - `cross-cutting` — ticket 3e3bc12a2040's branch (plain-ssh edge, SDK snapshot, workspace root); SDK clippy `result_large_err`; routine with `AWM_DIR` unset takes a scope as the workspace; the stale land lease; the shelved decisions (R79, per-node-edges, lyra vpn.toml, `isolated/` tag, alcor's sites daemon).
- `verify` — plan T9 on mizar and alcor. Check: dvc's mirror sends a dir holding only a symlink (e.g. `awm/` -> `data`) as a recursive Globus item; confirm Globus does not follow it into an excluded tree. From the test trim: gateway `stop` waits the full 8 s grace for a service that never registered; a crash-looping plugin may respawn once during a reload; cch's `shout_lost` log line is untested under load; the operations hook's dead-port retry is untested. From auth: the public profile has no live parity run, because only deneb serves it; `run.sh stop` leaves a sandbox's services running when the `dev` binary is not built.
- `later-nodes` — capella, lyra, deneb, shogun, altair; hpc-ssh-guard apply. deneb's `awm_plugins` must drop httpsfront in the same pass.

## Workers

Read-only survey subagents (Sonnet medium), one group at a time. Brief /home/tony/.claude/jobs/b8680c94/tmp/survey/COMMON.md; reports beside it as <group>.md. Done: ops.

Test trimmers (Sonnet medium, in this worktree, no commits), brief /home/tony/.claude/jobs/b8680c94/tmp/trim/BRIEF.md, started 2026-11-07: operations; dvc + awm-dvcdata; cch; harness + gateway + cli; the other landed crates.

## Closed

- `remote-build` — shelved (Tony: "lets shelf remote-build. we may not need it."). capella has 16 cores and 68 GB; mizar has 4.

- `operations` — live on alcor; see DECISIONS.md § ops-questions.
- `auth` — live on mizar and alcor; see DECISIONS.md § auth.
- `dvc` — merge t7 with operations; mizar live checks; altair and capella later.
- `queue-unblock` — no land queue this session; merge by hand, refresh mizar and alcor only.
- `inherited` — the original interview's decisions are the starting point.
