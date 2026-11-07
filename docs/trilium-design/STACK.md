# Trilium design — interview stack

Goal: settle the design of awm's Trilium surfaces with Tony: the swarm status page, the new-agent buttons, and the seat Remote Control toggle.
Decisions: `search` for tag `decision` in awm/trilium-design.

Related: journal 7724ff11 (ops-board decision), spec `docs/redux/*.md` (queue R61-R66, fleet R81/R105/R106, channels R77). Edit the spec one R-item at a time.

## Current

status-pages/new-agent-button

## Waiting on the user

(none)

## Preferences

- 2026-11-06: keep ceremony small; one decision journal per closed topic.
- 2026-11-06: one question per message, with a recommendation.
- 2026-11-06: Sonnet workers for straightforward builds; build for the end state, no compat shims.
- 2026-11-06 (via redux-endstate/review): message no session but the starter, and only with the final report. Answer no session except the starter. Take everything to Tony.
- 2026-11-06: land with routine(verb="deploy"); no PRs, no force-push. Never ssh to fir. Never print password note bodies.

## Stack

- status-pages — own questions closed (f421ff25; spec fleet.md R147). Build waits on its children. "ops can be the new swarm status page I requested and all of our nodes including stations register to it." Roster with stats per node; opt-in at awm install.
  - new-agent-button — "new note for proposed simplifies to new agent -> ... a button on the board page, which lets me start an agent with remote control on a specific node. nodes can be sorted by usage."
  - seat-rc-toggle — "on the new fleet status dashboard, I can turn remote control on and off for seats ... can we sort natively in claude?" Tony moved the whole topic here (seats/pause handoff). Seat context, settled in docs/seat-lifecycle (feat/seat-pause): no idle per-client agents; a DM goes to the seat's live agent, or an event routine starts one in agentic-commons seats/<person>; social holds seat state empty/occupied/retiring, reclaim after 4h done; names seat:slack/DM/<person>; reflection remote_control(job,on,name) works, refuses mid-turn. Open, with seats/pause recs: (a) dashboard reads `social seats` as source so empty seats show; (b) no passkey for RC toggle; (c) "sort natively in claude?" ours.
- note-agent-button — "a new button on trillium (left of the button, per note) that is a bot icon that opens a drop down for available nodes." Child note marks the attached agent; deleted on retire. Parallel to the task system.

- board-layout-followups — from redux-endstate/review after swarm-board/layout landed (awm d5be934c, journal 0d875664).
  - swarm-triage — the tasks/Swarm triage folder is empty: no queue section feeds it. What belongs there?
  - board-look — nobody has viewed the new page in a browser (passkey). Tony to look.

## Workers

- swarm-board/layout — landed awm d5be934c (journal 0d875664); done.
- seats/pause — on altair, bridge:session_012TZ1zzdZdBqRvgc4NA7dfK. Runs Tony's Slack seat redesign; owns the seat side of seat-rc-toggle. Not ours.

## Closed

(none)
