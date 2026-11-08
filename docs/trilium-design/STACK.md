# Trilium design — interview stack

Goal: settle the design of awm's Trilium surfaces with Tony: the swarm status page, the new-agent buttons, and the seat Remote Control toggle.
Decisions: `docs/trilium-design/DECISIONS.md`. Topics closed before 2026-11-08 also have a journal each (f421ff25, 46a610c5, 83668c7e, da0f35b6, 321e8ff2, b12f7bb4). One decision journal goes out when the stack empties.

Related: journal 7724ff11 (ops-board decision), spec `docs/redux/*.md` (queue R61-R66, fleet R81/R105/R106, channels R77). Edit the spec one R-item at a time.

## Current

PLAN

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

- PLAN — once the stack is empty: one formal plan for the whole build, then submit to plugin-redux/interview. Must fit plugin-redux ports: operations/front/routine in awm/port-ops, social in awm/port-comms (job e44d901a); the SDK carries a Rust `telemetry` library (today awm/service_components/telemetry, an event-sync layer, unused).

## Workers

- swarm-board/layout — landed awm d5be934c (journal 0d875664); done.
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
