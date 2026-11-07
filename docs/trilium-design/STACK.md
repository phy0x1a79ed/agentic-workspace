# Trilium design — interview stack

Goal: settle the design of awm's Trilium surfaces with Tony: the swarm status page, the new-agent buttons, and the seat Remote Control toggle.
Decisions: `search` for tag `decision` in awm/trilium-design.

Related: journal 7724ff11 (ops-board decision), spec `docs/redux/*.md` (queue R61-R66, fleet R81/R105/R106, channels R77). Edit the spec one R-item at a time.

## Current

signin-harmonize

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

- signin-harmonize — "I will try and harmonize the signin processes next as well, which may bring back some embedded frames." Tony leads. Context: page-origin first pass (da0f35b6, queue.md R64 item 5). Sign-ins today: vault passkey (R84), portal login grant via httpsfront (journal 9929bd81), approver passkey on auth.*, tailnet portal on alcor.
- note-agent-button — "a new button on trillium (left of the button, per note) that is a bot icon that opens a drop down for available nodes." Child note marks the attached agent; deleted on retire. Parallel to the task system. Partly settled by da0f35b6: it is an everyday action (deneb JS, reference only, agent waits on RC); "new task from this card" is the same button. Open from the spec sweep: child note outside writer roots; R106 item 3 "a session that needs Tony has a card".
- board-layout-followups — from redux-endstate/review after swarm-board/layout landed (awm d5be934c, journal 0d875664).
  - swarm-triage — the tasks/Swarm triage folder is empty: no queue section feeds it. What belongs there?
  - board-look — nobody has viewed the new page in a browser (passkey). Tony to look.
- PLAN — once the stack is empty: one formal plan for the whole build, then submit to plugin-redux/interview. Must fit plugin-redux ports: operations/front/routine in awm/port-ops, social in awm/port-comms (job e44d901a); the SDK carries a Rust `telemetry` library (today awm/service_components/telemetry, an event-sync layer, unused).

## Workers

- swarm-board/layout — landed awm d5be934c (journal 0d875664); done.
- seats/pause — on altair. Runs Tony's Slack seat redesign (docs/seat-lifecycle, feat/seat-pause); handed seat-rc-toggle to us. Not ours; do not message (standing instruction).

## Closed

- status-pages/page-origin — da0f35b6 (first pass). deneb serves all pages as notes; alcor = API backend + one identity page; device credential cookie; everyday actions carry no instruction/grant; R65 chain and frames gone. Supersedes the "operations owns the Ops page" answer.
- status-pages (own questions) — f421ff25. Spec fleet.md R147, install.md R115 item 4.
- seat-rc-toggle — 83668c7e. Spec fleet.md R147 item 7, sessions.md R146.
- new-agent-button — 46a610c5. Spec queue.md R61/R63/R65/R66, fleet.md R106/R147. Proposed dropped; scope awm/lobby.
