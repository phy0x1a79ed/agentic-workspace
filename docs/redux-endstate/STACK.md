# redux-endstate

Review the end state of the awm redux rollout on every node, and clean up what is left open. Protocol: the `dfs-interview` skill. Decisions: journals tagged `decision` in awm/redux-fixes (`search` "redux-endstate decision").

## Current

ops-board

## Waiting on Tony

- science-idle: the idle timeout for claude-science. Recommended: start on demand, stop after 30 min idle.

## Preferences

- Quote Tony only where his exact words are the decision. Never reprint the conversation. (2026-11-06)
- Use Sonnet workers for straightforward tasks, so this session stays free to coordinate. (2026-11-06)
- Keep the ceremony small: one file, written when a topic closes and before a compaction. (2026-11-06)

## Stack

- **ops-board**: "ops board needs updating"; "clean up the federation board and prepare for use".
  - Decided: the AWM book (`iF0A3SWEGqUx`) becomes the board's root: "AWM/tasks/working, etc/swarm triage. then ops and board page can be at level with tasks." Tony adds a card by making a note in a column folder. Ops (`awmSwarmOps`) is the swarm status page, every node and station registers to it (absorbs node-roster). Old Ops, its password notes and the old Swarm board are deleted; the 8 federation cards are deleted; Links sit under Ops.
  - Survey (2026-11-06): cards are flat under `awmSwarmBoard`, column = label `#section` (operations `writer.py:79-118`). The writer is write-only (R66): a hand-made note or a folder move never reaches the queue. Cards match the queue; they look stale because the card shows only title and chips, its reason and todos sit in the `record` child and the body is never rewritten. Layout code: `board.py` (ROOT_ID, install), `writer.py:96-101,171`, `hub_adapter.py:49,576-620`, `board.js:17-40`. Writer roots on the whole AWM book would open Major updates and the federation board to it.
  - **board-kanban**: "harmonizing with my tasks kanban, then not all cards will need an agent -> or we have a manual section". Tony's kanban is the Tasks book `EB6Mr07BifE5`: a folder-driven board (`#board:columnsFrom=folders`), columns Proposed, Planned, Active, Blocked, Pending review, Done, Cancelled.
- **end-state**: "review the end state and if there are anything left open for us to clean up and do."
  - **deneb-link**: mizar's peer link to deneb shows 'link dropped' since 06:12 (tailnet-sweep, 2026-11-06).
  - **telegram-token**: a Telegram bot token shows in `ps` on alcor (a bwrap command line). Rotate it and pass it by env file.
  - **skill-sync**: "we need a way to sync skills across the fleet..."
  - **agent-lifecycle**: "retire doesn seem to always work ... session full stop and closed (not visible anywhere), but transcripts kept (I guess transcripts should also be a plugin that runs on any node with agents)."
  - **agent-core**: "if a node can run agents, reflection + search should be mandatory". deneb has neither.
  - **seat-rc-toggle**: "on the new fleet status dashboard, I can turn remote control on and off for seats ... can we sort natively in claude?"
  - **science-idle**: "do we have an idle timeout to stop claude science from just being always on?"
  - **seat-lifecycle**: seats retire after the task with a report by DM, journal often, tags per user and one for seats; "the task should be resumable from debrief journals, even if the sessions are gone." Idle lab seats retire too.
  - **social-split**: social splits into write-only notifications, read/write channels (bots, seats) and personal automations (Teams, Slack browser). Browser joins core and keeps no browser warm.
  - **deploy-routine**: "change the deploy protocol to be deterministic with only agentic error handling". Spec pointer: journal e44b39dc.
  - **queue-approvals-test**: "a full test of the new queue and approvals workflow in 1 sitting, prolly with another agent".
  - **session-naming**: "naming by hand and omit agent renaming unless needed ... revise the naming scheme to include node."
  - **env-reload**: a changed config.toml [env] reaches plugins only after a full gateway restart, which stops the node's sessions. Tony: "can we make the gateway restart more seamless? what is it for anyways?" mizar's AWM_EDGE_URL (tailnet IP → name) and capella's new AWM_EDGE_URL wait on it.
  - **search-mcp**: "all nodes are using the new search service and MCP." Done except: stale scope worktrees carry a tracked `.mcp.json` naming the Python `awm-mcp` (22 on mizar, 77 on altair); a session started there gets a dead MCP.

## Workers

- fleet-endstate (daae3825, uds:/run/user/1000/cc-socks/226644.sock): shogun callers incl. trilium and zotero, capella service_key, Arbutus time zones.
- install-loop (9fc4afc8 mizar), metasmith-install-loop (80f06e0c altair): hold until Sat 2026-11-07 15:05 PT.

## Closed

- fir-ssh, refresh-all, node-time, nexus-signin, install-loop, shogun-singletons, session-cleanup, zerotier-to-tailnet: journal be712cac.
- zerotier-to-tailnet pass 2 (tailnet-sweep a2f9b042, cloud b2598dc on dev, journal 0f78d8bb): applied on alcor, deneb, capella (env only), altair in inventory. claude-science at https://altair.tail080e54.ts.net:12201 and https://alcor.tail080e54.ts.net:12201. Left on ZeroTier: ltz and macpro (10.74.81.208, .214), ZeroTier-only devices.
