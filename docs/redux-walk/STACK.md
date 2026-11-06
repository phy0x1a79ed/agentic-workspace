# redux-walk: stack

## Purpose

Take the redux build from its current state to the end state in docs/redux, one decided task at a time, with a top-level worker dispatched for each decided task

Procedure, from dfs-interview § Work the stack:

1. Run `stack.py <folder> current` for the top topic. Ask only its own questions.
2. Dispatch read-only subagents to survey what already exists for the topic.
3. Record each difference between the survey and the user's description.
4. Ask the user about each difference with an unclear answer.
5. Put one question per message to the user, or a small set of closely related questions. Give your recommendation with it.
6. Queue the other questions with `stack.py <folder> ask`.
7. Match each answer to the message it replies to. Ask when a reply could answer more than one message.
8. Record each answer in the topic's `README.md`. Copy the user's words from `quote.py`.
9. Write the result as the current design.
10. Tick each answered question with `stack.py <folder> answer`.
11. Run `stack.py <folder> close` when the topic's question queue is empty.
12. Commit.
13. Compact only when the closed topic ran long, with many exchanges or large reads. After a short topic, present the next topic in the same turn.
14. To compact, run `reflection(verb="compact", args={followup:...})`. Write a followup that invokes `dfs-interview` by name. Name the discussion folder, the closed topic, its commit and the new top topic in it.

## Current topic

**deneb-retry**. Its result: `deneb-retry/README.md`.

## Pending

- **rc-rows** (2026-11-05): On claude.ai/code, archive the 31 dead Remote Control rows: 7 redux-build-* (alcor, clean-plugins, search, channels, queue, deploy, integrate), 14 finished (awm redux, core-redux-gateway-plugins-bootstrap, fleet-board-rework, fxdiff-design, retire-full-close, owner-dm-defaults, discord dm previews, multi-admin machine access, plan-bypass-mode, science-bash-stall, capellaz cc mem cleanup, avatar, lit kb, cloud/homelab), representative x5, secretary x5. Your call on: engcyc pipeline plan, msm-*, fosdb, cyanoverse, treesapp, ecspr, job application system, 4 offline seat rows, 4 cloud sessions.

## Stack

- **tether-issue-door** (2026-11-05): from redux-build/dev: dev (2026-11-05): member nodes that open machine channels hold the relay's AWM_TETHER_ISSUE_TOKEN (mints slots and rooms, reads nothing). End state: relay gains a door-gated issue verb, tether-operator mints and restores through it, and the bearer never leaves deneb. Cross-project (tether Rust release), so not in tonight's window.
- **rt-teach** (2026-11-05): Tony (2026-11-05): we need the rooms service to be up so that the Alcor agent can teach the RT agent, which was the original session that created the RT stock stuff, how to actually use the new system and edit.
  - [x] Which machine and session was the original RT agent? rt-container found no trace on mira or mizar. Leads point to your Mac, or capella or lyra over ssh. (answered 2026-11-05)
  - **rooms** (2026-11-05): from redux-build/dev: dev (2026-11-05): the RC does not support the rooms check: the door carries no caller session, members receive only by an outbound push from deneb (R79), isolated sessions cannot reach a door singleton, a room has no second member, and remote holders are never pruned. About a day of build.
    - [x] Does the door-client window wait for rooms? Recommended: no. Run the window when the RC is green; rooms gate the mira-to-alcor rt move, where the alcor agent teaches the rt agent. (answered 2026-11-05)
- **board-intake-invert** (2026-11-05): from redux-build/dev: dev (2026-11-05): board intake (R77) needs operations on alcor, so with the board host on deneb intake never delivers; events queue in deneb's board log. Inverted shape: alcor's operations pulls from deneb's board door. R77's door and intake are slated for deletion (T18 feat/redux-r77).
- **zotero-order-invert** (2026-11-05): from redux-build/dev: dev (2026-11-05): with R79, zotero's Sync now (R83) never runs on deneb, because deneb's zotero long-polls alcor's approver (order_pull), which is outbound. Invert it: alcor's approver calls a zotero verb on deneb's door with the signed envelope.
- **deneb-retry** (2026-11-05): from redux-build/deneb-board: deneb-board (2026-11-05): the cutover failed after the vault move. f5c5294d refuses deneb's plugins because they need operations/social, which only door-client lets a node meet through a singleton's door. Rolled back; 2b (sshd/ufw/SNI) kept. The retry needs a release with door-client.
- **mizar-backup** (2026-11-05): from redux-build/rt-container: rt-container (2026-11-05): mizar had no dvc binary; data_dvc's fallbacks look under ~/lib/miniforge3 but mizar's miniforge is ~/miniforge3, so scope create/heal report mode none. Both nightly chinook jobs on mizar (cache_sync, workspace_backup) fail: .awm/dvc.toml [chinook] is missing, so nothing on mizar is backed up off-site.
- **dispatch-mode** (2026-11-05): as tasks are decided and worked out, please dispatch top level agents to work on them / their issues can be pushed back to the stack / we will notify them once their issues are solved and how, rather than when we decide how to solve. / we can add this mode to dfs-interview skill
  - **parallel-uptime** (2026-11-05): I'd like to be able to work in parallel. if all discussion goes through you, then I sit idle while you work and vice versa. our first topic should be a protocol taht maximizes both of our up times
    - **board** (2026-11-05): the "One file holds everything" is not a hard blocker, can we look into setting up trillium and the new board then? this would include the alcor services
      - [x] Does alcor open for the board's services only (operations, approver and passkey, deneb tunnel), with rt, IBKR and mira still shut? (answered 2026-11-05)
      - [x] Tony's DNS steps M10, M12, M13, M14: can he do them now, in parallel? (answered 2026-11-05)
      - [x] Discord on alcor: hold social's Discord accounts on alcor until mira's cutover? (answered 2026-11-05)
      - **door-client** (2026-11-05): from redux-build/dev: alcor-open, needs your decision: 20 plugins are refused on alcor. The root is trilium. ... The gateway has no client for a peer's singleton door, so it lists peer tools only from edges.
        - [ ] Tell alcor-open to resume step 6 (approver check) and any role held for operations when the door-client release lands
        - [ ] Deploy door-client only after deneb's door answers with service keys (keys role from deneb-board; dev applies on mizar, alcor-open on alcor)
        - [ ] After door-client is live: remove mizar's ZOTERO_SYNC_ENABLED=0 drop-in and zotero from every node but deneb
      - **alcor-door** (2026-11-05): from redux-build/alcor-open: alcor_door: FIREWALL on port 22. It allows 22 from mizar 100.125.95.5, capella 100.90.179.70 and lyra 100.85.115.29, then deletes the blanket 22 rules ... After it, port 22 on alcor answers only those three tailnet addresses. No LAN ssh, no phone.
      - **deneb-board** (2026-11-05): trilium-fork follow-ups: deneb gets the tarball only once cloud's install config makes deneb a push node; cloud deploy.yml awm_fleet_push.altair still lists trilium and canonical_repos defaults include trilium; the mizar vault mirror does not exist yet
        - [x] Why does deneb need nodejs, npm and build-essential if mizar pushes built artifacts? (asked deneb-board) (answered 2026-11-05)
        - [x] When core-artifact lands: tell deneb-board the artifact names and route (cloud tip 88c35a8 holds) (answered 2026-11-05)
- **path-front-door** (2026-11-05): Tony raised a path-based front door (nexus.tony-xy-liu.com/<node>/...) as a possible later design; not decided. (via dns-guide, 2026-11-05)

## Parked

From journal 6a98c0c6 (awm/redux) and walkthrough's handover (manual-steps.md at feat/redux-walkthrough a12a5f6e, M-numbers). Live workers: dev, singleton-callers, rt-container, reflect-selfstop.

- lyra: run awm-wsl-bridge-lyra.ps1 on xps-laptop (M18), ssh 100.85.115.29 lands in WSL, then start WSL (M41)
- tailnet: altair joins (M21), Tailnet Lock (M22), ACL from tailnet.yml (M36)
- shogun: Ryan's keys (M6, M40), auth key and lock-sign (M35), app and wake credentials (M37, M38), its key into altair (M39); shogun stays planned
- goal.md and the ~/.claude drafts: confirm the CLAUDE.md standing frame (M4, goal 50708f67), then install
- agentic-commons: private repo on phy0x1a79ed plus a Hallam lab fork (M8)
- cloud repo: canonical home on mizar and a GitHub backup; cloud dev is 58339c4 on mizar, local only, guarded container run green (dev, 2026-11-05)
- build-node odds: qemu binfmt (M2), push approvals (M23), /mcp reconnect on deployed nodes (M24), cloud env network level (M53), bookmarks to /p/<plugin>/ (M54)
- avatar stage client port (M11), needs capella awake
- DNS and portal: auth A record, CAA, DNS token and ACME email (M12-M14), cutover check channel (M7), portal hosts (M10)
- alcor sitting: deploy, real user CA, Chrome logins, Duo, passkey, Ansible apply (M19, M25-M29, M33, M42-M52)
- mira: rt-container's design (feat/rt-container abbe1ac holds the stock-market and rt callers; merge only with the stock plugins; the plugin build waits on Tony's go to rt-container), IBKR (M30), cutover play (M31), capella debug port (M32), held until Tony opens mira and alcor
- Discord seats (M34)
- R77 after the Caviar and muse invites (M55)
- reflection follow-ups from reflect-selfstop (release dcd8ba4d, journal 8542e517): cch status misses a session by its post-clear id, so stop {job:<new id>} misses it; old trust.json records have no job field
- altair still commits nightly vault snapshots to its fork release; R105 stops that (trilium-fork, 2026-11-05)
- search-awm-api follow-ups (journal e06acc4b): `search sources` counts awm docs for singletons a relayed caller cannot see; a stale `_grants` line in search INSTALL.md
- spawn-tuning follow-ups (journal 0fc35624): cch test_stop_refuses_a_caller_whose_mode_is_unknown fails on 9d346fb4 too; run-tests.sh does not run the operations plugin tests
- core-artifact follow-up (journal 88a132af): pages of an out-of-tree plugin (AWM_PLUGIN_ROOTS, such as the optional plugin projects of R33) are not in pages.pin, so no push node gets them
- homelab_base "Fetch Docker's apt key" reports a change on every run, so the role is not idempotent (alcor-open, 2026-11-05)
- end state: verify every node (T28), retire mira and its disk (T29, M56), debrief (T30)
- mizar off-site data backup: chinook is ruled out (lab resource) and mizar's chinook jobs are disabled; pick another target later (Tony, 2026-11-05)

## Workers

- **redux-build/trilium-fork (job e74cd7f2): the fork on mizar, its first release tarball, server.pin** (2026-11-05): took board
- [x] tell redux-build/trilium-fork how tarball-host was solved (told 2026-11-05)
- **redux-build/alcor-open (job 839aad63): alcor onto dcd8ba4d and its roles, dry run first, rt, IBKR, mira and Discord held** (2026-11-05): took board
- **redux-build/dns-guide (job 67cbdc6d, sonnet medium): guides Tony through M10, M12-M14** (2026-11-05): took board
- **note: users_sudo on alcor held as Tony's M29 (ssh admin gate); social on alcor runs with no accounts** (2026-11-05): took dispatch-mode
- **redux-build/deneb-board (job a4d345a8): deneb push node, trilium leftovers, deneb dry run; apply waits on Q-deneb** (2026-11-05): took deneb-board
- **redux-build/search-awm-api (job 8790d479, opus high)** (2026-11-05): took search-awm-api
- **redux-build/spawn-tuning (job 716a0319, sonnet high)** (2026-11-05): took spawn-tuning
- [x] tell redux-build/deneb-board how trilium-shape was solved (told 2026-11-05)
- **redux-build/core-artifact (job 96e615ee, sonnet high): pinned musl core binary for push nodes (from dev's deneb route answers)** (2026-11-05): took deneb-board
- **redux-build/door-client (job 48198ebe, opus high): gateway client for a peer's singleton door; needs check counts door-routed tools as declared** (2026-11-05): took door-client
- **redux-build/deneb-singletons (job 8e4c71f0, opus high): trilium refuses an empty vault, zotero through the trilium tool, per-singleton secrets and tether paths** (2026-11-05): took deneb-gaps
- **redux-build/pin-refresh (job b51aabfa, sonnet high): routine deploy rebuilds stale core and page pins** (2026-11-05): took pin-refresh
- [x] tell redux-build/dev how zotero-home was solved (told 2026-11-05)
- [x] tell redux-build/deneb-board how deneb-gaps was solved (told 2026-11-05)
- [x] tell redux-build/dev how pin-refresh was solved (told 2026-11-05)
- **redux-build/session-cleanup (job 79aa5482, sonnet medium): inventory and cleanup only; the listing design is for discussion** (2026-11-05): took session-list
- **redux-build/dev** (2026-11-05): took mizar-backup
- **redux-build/deneb-board** (2026-11-05): took tarball-backup
- **redux-build/rooms** (2026-11-05): took rooms

## Closed

- **tarball-host** (closed 2026-11-05): `dispatch-mode/parallel-uptime/board/tarball-host/README.md`
- **search-awm-api** (closed 2026-11-05): `search-awm-api/README.md`
- **spawn-tuning** (closed 2026-11-05): `spawn-tuning/README.md`
- **trilium-shape** (closed 2026-11-05): `dispatch-mode/parallel-uptime/board/deneb-board/trilium-shape/README.md`
- **zotero-home** (closed 2026-11-05): `dispatch-mode/parallel-uptime/board/deneb-board/zotero-home/README.md`
- **deneb-gaps** (closed 2026-11-05): `dispatch-mode/parallel-uptime/board/deneb-board/deneb-gaps/README.md`
- **pin-refresh** (closed 2026-11-05): `dispatch-mode/parallel-uptime/board/deneb-board/pin-refresh/README.md`
- **session-list** (closed 2026-11-05): `session-list/README.md`
- **tarball-backup** (closed 2026-11-05): `dispatch-mode/parallel-uptime/board/tarball-backup/README.md`
