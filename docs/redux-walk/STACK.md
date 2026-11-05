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

**alcor-door**, under **board**. Its result: `dispatch-mode/parallel-uptime/board/alcor-door/README.md`.

## Pending

- **M10** (2026-11-05): Name the portal hostname your browser uses for mizar, alcor, altair, capella and lyra, or none. Suggested for mizar: mizar.tail080e54.ts.net. dns-guide walks you through it.
- **M12** (2026-11-05): Cloudflare: add A record auth -> 43.162.91.185, DNS only. dns-guide checks it.
- **M13** (2026-11-05): Cloudflare: add CAA record auth = 0 issue "letsencrypt.org; validationmethods=dns-01". Needs M12.
- **M14** (2026-11-05): Cloudflare: create an API token, Zone DNS Edit, zone tony-xy-liu.com only; keep it in your password manager until M25. Give an ACME email or none. Needs M12.
- **rt-plugins** (2026-11-05): rt-container waits on your go to build the stock plugins (projects/stock/plugins). Talk to it directly.
- **Q-tarball-backup** (2026-11-05): Trilium fork backup: push its branches and tags to the existing GitHub fork phy0x1a79ed/Trilium, and attach the pinned tarball to a GitHub release there as a backup copy only (deneb still gets mizar's push)? Recommended: yes.
- **Q-alcor-door** (2026-11-05): alcor's port 22 lockdown: after it, ssh to alcor works only from mizar, capella and lyra over the tailnet. No LAN ssh, no phone, no altair. OK, or should more devices (your phone, by tailnet address) stay allowed?
- **Q-deneb** (2026-11-05): deneb for the board: after a dry run I review, apply deneb's roles: SNI forward to alcor, push node, passkey login, and Trilium moved to its own sandboxed user (fleet.md R105 item 1). That moves the live vault: stop Trilium, back up document.db to mizar, move the data, start, check. OK?
- **Q-seat-models** (2026-11-05): Seats: after spawn-tuning, each Slack/Discord seat names its model and effort. dev sets them to today's opus at high. Want any seat lighter, such as sonnet at medium for plain Q&A seats? Optional, no rush.

## Stack

- **dispatch-mode** (2026-11-05): as tasks are decided and worked out, please dispatch top level agents to work on them / their issues can be pushed back to the stack / we will notify them once their issues are solved and how, rather than when we decide how to solve. / we can add this mode to dfs-interview skill
  - **parallel-uptime** (2026-11-05): I'd like to be able to work in parallel. if all discussion goes through you, then I sit idle while you work and vice versa. our first topic should be a protocol taht maximizes both of our up times
    - **board** (2026-11-05): the "One file holds everything" is not a hard blocker, can we look into setting up trillium and the new board then? this would include the alcor services
      - [x] Does alcor open for the board's services only (operations, approver and passkey, deneb tunnel), with rt, IBKR and mira still shut? (answered 2026-11-05)
      - [x] Tony's DNS steps M10, M12, M13, M14: can he do them now, in parallel? (answered 2026-11-05)
      - [x] Discord on alcor: hold social's Discord accounts on alcor until mira's cutover? (answered 2026-11-05)
      - **door-client** (2026-11-05): from redux-build/dev: alcor-open, needs your decision: 20 plugins are refused on alcor. The root is trilium. ... The gateway has no client for a peer's singleton door, so it lists peer tools only from edges.
        - [ ] Tell alcor-open to resume step 6 (approver check) and any role held for operations when the door-client release lands
      - **alcor-door** (2026-11-05): from redux-build/alcor-open: alcor_door: FIREWALL on port 22. It allows 22 from mizar 100.125.95.5, capella 100.90.179.70 and lyra 100.85.115.29, then deletes the blanket 22 rules ... After it, port 22 on alcor answers only those three tailnet addresses. No LAN ssh, no phone.
      - **tarball-backup** (2026-11-05): trilium-fork: mizar's .awm/artifacts holds the only copy of the pinned tarball. A rebuild changes the sha256, so it needs a backup policy. Nothing is pushed to the GitHub fork. That waits on your go.
      - **deneb-board** (2026-11-05): trilium-fork follow-ups: deneb gets the tarball only once cloud's install config makes deneb a push node; cloud deploy.yml awm_fleet_push.altair still lists trilium and canonical_repos defaults include trilium; the mizar vault mirror does not exist yet

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
- end state: verify every node (T28), retire mira and its disk (T29, M56), debrief (T30)

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

## Closed

- **tarball-host** (closed 2026-11-05): `dispatch-mode/parallel-uptime/board/tarball-host/README.md`
- **search-awm-api** (closed 2026-11-05): `search-awm-api/README.md`
- **spawn-tuning** (closed 2026-11-05): `spawn-tuning/README.md`
- **trilium-shape** (closed 2026-11-05): `dispatch-mode/parallel-uptime/board/deneb-board/trilium-shape/README.md`
