# Trilium design — interview stack

Goal: settle the design of awm's Trilium surfaces with Tony: the swarm status page, the new-agent buttons, and the seat Remote Control toggle.
Decisions: `search` for tag `decision` in awm/trilium-design.

Related: journal 7724ff11 (ops-board decision), spec `docs/redux/*.md` (queue R61-R66, fleet R81/R105/R106, channels R77). Edit the spec one R-item at a time.

## Current

status-pages (page owner question, then dispatch the build)

## Waiting on the user

(none)

## Preferences

- 2026-11-06: keep ceremony small; one decision journal per closed topic.
- 2026-11-06: one question per message, with a recommendation.
- 2026-11-06: Sonnet workers for straightforward builds; build for the end state, no compat shims.
- 2026-11-06 (via redux-endstate/review): message no session but the starter, and only with the final report. Answer no session except the starter. Take everything to Tony.
- 2026-11-06: land via the ci/land skill (scripts/land.sh submit on mizar); no PRs, no force-push. Never ssh to fir. Never print password note bodies.

## Stack

- status-pages — own questions closed (f421ff25; spec fleet.md R147). Children closed. Open: which plugin owns the Ops page. Then dispatch the build. Spec sweep 2026-11-07: Ops page has no owning plugin yet (R115 item 1: a page is a plugin). "ops can be the new swarm status page I requested and all of our nodes including stations register to it." Roster with stats per node; opt-in at awm install.
- note-agent-button — "a new button on trillium (left of the button, per note) that is a bot icon that opens a drop down for available nodes." Child note marks the attached agent; deleted on retire. Parallel to the task system.

- board-layout-followups — from redux-endstate/review after swarm-board/layout landed (awm d5be934c, journal 0d875664).
  - swarm-triage — the tasks/Swarm triage folder is empty: no queue section feeds it. What belongs there?
  - board-look — nobody has viewed the new page in a browser (passkey). Tony to look.

## Workers

- swarm-board/layout — landed awm d5be934c (journal 0d875664); done.
- seats/pause — on altair. Runs Tony's Slack seat redesign (docs/seat-lifecycle, feat/seat-pause); handed seat-rc-toggle to us. Not ours; do not message (standing instruction).

## Closed

- status-pages (own questions) — f421ff25. Spec fleet.md R147, install.md R115 item 4.
- seat-rc-toggle — 83668c7e. Spec fleet.md R147 item 7, sessions.md R146.
- new-agent-button — 46a610c5. Spec queue.md R61/R63/R65/R66, fleet.md R106/R147. Proposed dropped; scope awm/lobby.
