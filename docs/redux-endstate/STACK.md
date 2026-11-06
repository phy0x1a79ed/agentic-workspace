# redux-endstate

Review the end state of the awm redux rollout on every node, and clean up what is left open. Protocol: the `dfs-interview` skill. Decisions: journals tagged `decision` in awm/redux-fixes (`search` "redux-endstate decision").

## Current

end-state/deneb-link

## Waiting on Tony

- deneb-link: let the edge key log in as awm on deneb, or drop the edge.
- science-idle: the idle timeout for claude-science. Recommended: start on demand, stop after 30 min idle.

## Preferences

- Quote Tony only where his exact words are the decision. Never reprint the conversation. (2026-11-06)
- Use Sonnet workers for straightforward tasks, so this session stays free to coordinate. (2026-11-06)
- Keep the ceremony small: one file, written when a topic closes and before a compaction. (2026-11-06)

## Stack

- **end-state**: "review the end state and if there are anything left open for us to clean up and do."
  - **deneb-link**: mizar's edge to deneb fails since ~06:05Z. The edge is `ssh deneb awm edge-serve`; it lands as tony, who has no awm on deneb (deneb's awm runs as user awm). Fix needs the edge key to log in as awm on the public station (R79/R88), or drop mizar→deneb from edges.yml. Waiting on Tony. The awm-edge-fwd forwards and peers.json edge_urls are ZeroTier-era leftovers to retire.
  - **seats-pause**: "retired seats came back and steven chen contacted the client without first being asked, on a stale request." Five revived seats stopped ~19:10Z; seats/pause (e11ce259 on altair) keeps them paused, finds the cause and discusses seats with Tony (takes seat-lifecycle).
  - **telegram-token**: a Telegram bot token shows in `ps` on alcor (a bwrap command line). Tony deletes it if rt no longer uses Telegram; a subagent checks.
  - **skill-sync**: "we need a way to sync skills across the fleet..."
  - **agent-lifecycle**: "retire doesn seem to always work ... session full stop and closed (not visible anywhere), but transcripts kept (I guess transcripts should also be a plugin that runs on any node with agents)."
  - **agent-core**: "if a node can run agents, reflection + search should be mandatory". deneb has neither.
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
- swarm-board/layout (8e2d9201): builds the ops-board decision (journal 7724ff11).
- trilium-design/interview (c6adc424, talks with Tony directly): status-pages, new-agent-button, note-agent-button, seat-rc-toggle.
- seats/pause (e11ce259, altair, talks with Tony directly).
- install-loop (9fc4afc8 mizar), metasmith-install-loop (80f06e0c altair): hold until Sat 2026-11-07 15:05 PT.

## Closed

- ops-board, board-kanban: journal 7724ff11.
- fir-ssh, refresh-all, node-time, nexus-signin, install-loop, shogun-singletons, session-cleanup, zerotier-to-tailnet: journal be712cac.
- zerotier-to-tailnet pass 2 (tailnet-sweep a2f9b042, cloud b2598dc on dev, journal 0f78d8bb): applied on alcor, deneb, capella (env only), altair in inventory. claude-science at https://altair.tail080e54.ts.net:12201 and https://alcor.tail080e54.ts.net:12201. Left on ZeroTier: ltz and macpro (10.74.81.208, .214), ZeroTier-only devices.
