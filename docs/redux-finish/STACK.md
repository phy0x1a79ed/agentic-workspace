# Redux finish — stack

Goal: bring mizar and alcor to the post-redux state in this one session, from the work the plugin-redux workers left. Other nodes wait. Prompt (2026-11-07): "can we finish the work here, in 1 session? … Lets get mizar (and alcor since it holds crucial services) to the post redux state before worring about other nodes."

Decisions: `DECISIONS.md` beside this file. Issues for Tony to read at any time: `ISSUES.md` beside this file.

Sources: the worker debrief journals 389a0111 (ops), 8caec34d (knowledge), 449af06d (harness), 004f4c13 (comms), f9c57c47 (trilium), 4bbcc79a (hpc-ssh-guard), 3781f166 (coordinator), and the handoffs they name.

## Current

- `queue-unblock` — the land queue is held by the dead batch cffff968f16f. Closing it needs a rollout choice: narrow `[fleet] nodes` to mizar and alcor, or roll on to the rest.

## Waiting on the user

- `queue-unblock`: narrow the rollout to mizar and alcor? (asked 2026-11-07)

## Preferences

- 2026-11-07: one session does the work. Sonnet-medium subagents take well-defined tasks.
- 2026-11-07: track the walk with dfs-interview. New issues go to `ISSUES.md`, not a question each turn.
- 2026-11-07: do not use the board; it is being remade. reflection and journals work.
- 2026-11-07: mizar and alcor first. Other nodes later.
- 2026-11-07: start from the original interview's decisions (DECISIONS.md § inherited). Work plugin by plugin. Related issues are prioritized case by case.

## Stack

Plugin by plugin, in land order. Each topic: finish what is left, land, live-check on mizar and alcor.

- `queue-unblock` — close batch cffff968f16f; decide the rollout set.
- `dvc` — ticket 68647284ec63 (feat/port-ops-t7) is ready; T7 live checks.
- `operations` — feat/port-ops (built, 212 tests) + cloud feat/ops-units on alcor. Needs the cloud dev merge first.
- `trilium-board` — the vault board and Ops, unfinished. Telemetry landed (acadb279); vault feat/trilium-vault 92a7d431 built, not submitted; cloud feat/telemetry-optin unrun (telemetry singleton on alcor); R79 decision first. Needs the Rust operations, the vault API and reflection's `reflection/sessions`.
- `social` + `browser` — feat/port-comms + cloud feat/rust-singletons on alcor.
- `channel` — on feat/port-comms; relay half on deneb (later-nodes).
- `rlm-browser` — on feat/port-comms.
- `kb` — journal, search, transcripts folded in; feat/port-knowledge untested; cloud feat/kb-journal-unit.
- `scopes` — feat/port-knowledge, after kb.
- `reflection` — wip/port-harness-reflection; dsh session events ride with it; order-puller handover with operations.
- `auth` — feat/port-ops-t9, about half ported (front: auth + httpsfront + tlsfront).
- `python-out` — plan T8: drop the core Python install path; non-core Python plugins get their own envs.
- `verify` — plan T9 on mizar and alcor.
- `later-nodes` — capella, lyra, deneb, shogun, altair; hpc-ssh-guard apply.

Related, taken case by case: ticket 3e3bc12a2040 (plain-ssh edge, SDK snapshot, workspace root), the SDK clippy lint, statecheck's directory extra, the shelved decisions.

## Workers

(none)

## Closed

- `inherited` — the original interview's decisions are the starting point.
