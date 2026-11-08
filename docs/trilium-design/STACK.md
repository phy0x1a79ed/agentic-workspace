# Trilium design — interview stack

Goal: settle the design of awm's Trilium surfaces with Tony: the swarm status page, the new-agent buttons, and the seat Remote Control toggle.
Decisions: `docs/trilium-design/DECISIONS.md`. Topics closed before 2026-11-08 also have a journal each (f421ff25, 46a610c5, 83668c7e, da0f35b6, 321e8ff2, b12f7bb4). One decision journal goes out when the stack empties.

Related: journal 7724ff11 (ops-board decision), spec `docs/redux/*.md` (queue R61-R66, fleet R81/R105/R106, channels R77). Edit the spec one R-item at a time.

## Current

BUILD — PLAN.md approved 2026-11-07. T1 done (CONTRACT.md, cf30928e; adds a gateway item for awm/services and awm/usage). T2 done: workers plugin-redux/trilium-telemetry (job 4196b8cb, scope awm/trilium-telemetry, Opus high) and plugin-redux/trilium-vault (job daf676e8, scope awm/trilium-vault, Sonnet high), both from feat/trilium-design at cf30928e. Flat scopes, because a branch feat/trilium-design/<x> cannot sit beside feat/trilium-design. Next: wait for both reports, then T6.

## Waiting on the user

(none)

## Preferences

- 2026-11-06: keep ceremony small; one decision journal per closed topic.
- 2026-11-06: one question per message, with a recommendation.
- 2026-11-06: Sonnet workers for straightforward builds; build for the end state, no compat shims.
- 2026-11-06 (via redux-endstate/review): message no session but the starter, and only with the final report. Answer no session except the starter. Take everything to Tony.
- 2026-11-07: before any build, submit a formal plan (planning style) to Tony. Build only in this scope (awm/trilium-design), so nothing can collide. Last step: submit the work to plugin-redux/interview, which merges it with its ports. Tony: "please submit formal plan before starting and work in your own scope so you literally can not collide. the last step would be to just submit work to plugin-redux/interview agent, who will merge along with their work".
- 2026-11-06: no PRs, no force-push. Never ssh to fir. Never print password note bodies.

## Stack

- (empty: every topic closed; the build follows docs/trilium-design/PLAN.md)

## Workers

- plugin-redux/interview — the coordinator this session now works for, as plugin-redux/port-trilium (Tony, 2026-11-07: "talk to plugin redux interview, please essentially register yourself as one of their workers and rename accordingly"). Answered 2026-11-07: C1 is ours (it tells port-ops; never message port-ops). Each nested scope lands via scripts/land.sh submit --brief <file> --restart <names>; contract items go to plugin-redux/interview, not in this branch, one per port. Base on release (38bcdafe has the SDK). Plugin up/down already emits on awm/plugins. Build only via scripts/rust-build.sh; --gc --yes after each phase. Features only in core/Cargo.toml. Ask before adding a store/index engine (c2419412). deploy.toml pin + describe baseline in the adding commit. Push to `github`, not `origin`. trilium keeps its own install hook and env; statecheck first if it rewrites state. All folded into PLAN.md.
- swarm-board/layout — landed awm d5be934c (journal 0d875664); done.
- Session renamed plugin-redux/port-trilium 2026-11-07.
- seats/pause — on altair. Runs Tony's Slack seat redesign (docs/seat-lifecycle, feat/seat-pause); handed seat-rc-toggle to us. Not ours; do not message (standing instruction).

## Closed

Decisions: DECISIONS.md.

- status-pages
- new-agent-button
- seat-rc-toggle
- page-origin (first pass)
- signin-harmonize
- note-agent-button
- swarm-triage
- board-look
