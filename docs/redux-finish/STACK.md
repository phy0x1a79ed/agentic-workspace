# Redux finish — stack

Goal: bring mizar and alcor to the post-redux state in this one session, from the work the plugin-redux workers left. Other nodes wait. Prompt (2026-11-07): "can we finish the work here, in 1 session? … Lets get mizar (and alcor since it holds crucial services) to the post redux state before worring about other nodes."

Decisions: `DECISIONS.md` beside this file. Questions and issues live in this file's stack, under the plugin they belong to.

Sources: the worker debrief journals 389a0111 (ops), 8caec34d (knowledge), 449af06d (harness), 004f4c13 (comms), f9c57c47 (trilium), 4bbcc79a (hpc-ssh-guard), 3781f166 (coordinator), and the handoffs they name.

## Current

- `scoping` / `operations` — survey in (survey/ops.md). Asked Tony: the vault board freezes between the Rust operations and the trilium writer; apply ops-units without the deneb caller until R79 is settled; Tony at hand for the passkey checks.

## Waiting on the user

- `operations`: board freeze OK; deneb caller deferred to trilium-board; passkey checks timing. (asked 2026-11-07)

## Preferences

- 2026-11-07: one session does the work. Sonnet-medium subagents take well-defined tasks.
- 2026-11-07: track the walk with dfs-interview. Questions and issues go on the stack; no separate issues file (Tony: "issues.md is redundant").
- 2026-11-07: do not use the board; it is being remade. reflection and journals work.
- 2026-11-07: mizar and alcor first. Other nodes later.
- 2026-11-07: start from the original interview's decisions (DECISIONS.md § inherited). Work plugin by plugin. Related issues are prioritized case by case.
- 2026-11-07: scope every plugin in the interview first; no work before its scope is settled. Survey and discuss one set at a time.

## Stack

Phase 1 scopes each plugin; phase 2 does the work in this order. Each topic lists its known questions and issues.

- `scoping` — the plugin-by-plugin walk.
  - `operations` — feat/port-ops (212 tests, no code left) + cloud feat/ops-units on alcor. Merge conflicts: deploy-hooks.sh, Cargo.lock, deploy.toml, fleet.md. Edges still redirect to `/portal_login` (now 404) until `operations,httpsfront` restart; cloud `deneb_passkey_login_url` still names `/portal_login`. Issues: the cloud dev drift (github 8f79bec ahead of mizar's bare 6c01e03); `run-containers.sh` lacks the community.docker collection; two order pullers once reflection lands.
  - `opt-in` — Tony: "an opt in mechanism for prototype features like the deploy queue and the board. this way we can try features in a few select sessions rather than fleet wide". Before trilium-board, its first user.
  - `trilium-board` — telemetry landed (acadb279); vault feat/trilium-vault 92a7d431 not submitted; cloud feat/telemetry-optin unrun; R79 (deneb's outbound calls) first. Needs the Rust operations, the vault API and reflection's `reflection/sessions`.
  - `social` + `browser` — feat/port-comms + cloud feat/rust-singletons on alcor. Seats stay paused until Tony turns them on.
  - `channel` — on feat/port-comms; the relay half runs on deneb.
  - `rlm-browser` — on feat/port-comms.
  - `kb` — journal, search, transcripts folded in; feat/port-knowledge passes cargo check only, journals has no tests, clippy fails; cloud feat/kb-journal-unit; model files; the agent-comms tag clash between mizar and alcor.
  - `scopes` — feat/port-knowledge, after kb.
  - `reflection` — wip/port-harness-reflection compiles, tests unfinished; dsh session events ride with it; the order-puller handover with operations.
  - `auth` — feat/port-ops-t9 does not build: ~3,600 lines of Rust written (certs, tls done), ~3,500 lines of Python left (proxy core, httpsfront app, wiring) plus 257 tests and a live-parity matrix. Size L. auth.tony-xy-liu.com on alcor is nginx + tunnel to deneb, not this plugin.
  - `python-out` — plan T8: drop the core Python install path; each non-core Python plugin gets its own env.
  - `landed` — cch, dsh, dev, mic, fileviewer, routine, dashboard, telemetry: confirm each is healthy on mizar and alcor. Issue: statecheck cannot copy a directory `[[extra]]` (took mizar's cch down 4 minutes).
  - `cross-cutting` — ticket 3e3bc12a2040's branch (plain-ssh edge, SDK snapshot, workspace root); SDK clippy `result_large_err`; routine with `AWM_DIR` unset takes a scope as the workspace; the stale land lease; the shelved decisions (R79, per-node-edges, lyra vpn.toml, `isolated/` tag, alcor's sites daemon).
- `verify` — plan T9 on mizar and alcor.
- `later-nodes` — capella, lyra, deneb, shogun, altair; hpc-ssh-guard apply.

## Workers

Read-only survey subagents (Sonnet medium), one set at a time (Tony: "1 set at a time"). Brief /home/tony/.claude/jobs/b8680c94/tmp/survey/COMMON.md; reports beside it as <set>.md. Running: ops (dvc, operations, auth), started 2026-11-07. The first six were killed by the /cd move.

## Closed

- `dvc` — merge t7 with operations; mizar live checks; altair and capella later.
- `queue-unblock` — no land queue this session; merge by hand, refresh mizar and alcor only.
- `inherited` — the original interview's decisions are the starting point.
