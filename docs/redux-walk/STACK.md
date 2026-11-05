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

**board**, under **parallel-uptime**. Its result: `dispatch-mode/parallel-uptime/board/README.md`.

## Stack

- **dispatch-mode** (2026-11-05): as tasks are decided and worked out, please dispatch top level agents to work on them / their issues can be pushed back to the stack / we will notify them once their issues are solved and how, rather than when we decide how to solve. / we can add this mode to dfs-interview skill
  - **parallel-uptime** (2026-11-05): I'd like to be able to work in parallel. if all discussion goes through you, then I sit idle while you work and vice versa. our first topic should be a protocol taht maximizes both of our up times
    - **board** (2026-11-05): the "One file holds everything" is not a hard blocker, can we look into setting up trillium and the new board then? this would include the alcor services
      - [x] Does alcor open for the board's services only (operations, approver and passkey, deneb tunnel), with rt, IBKR and mira still shut? (answered 2026-11-05)
      - [ ] Tony's DNS steps M10, M12, M13, M14: can he do them now, in parallel?
      - [ ] Discord on alcor: hold social's Discord accounts on alcor until mira's cutover?

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
- mira: rt-container's design, IBKR (M30), cutover play (M31), capella debug port (M32), held until Tony opens mira and alcor
- Discord seats (M34)
- R77 after the Caviar and muse invites (M55)
- reflection follow-ups from reflect-selfstop (release dcd8ba4d, journal 8542e517): cch status misses a session by its post-clear id, so stop {job:<new id>} misses it; old trust.json records have no job field
- end state: verify every node (T28), retire mira and its disk (T29, M56), debrief (T30)

## Workers

- **redux-build/trilium-fork (job e74cd7f2): the fork on mizar, its first release tarball, server.pin** (2026-11-05): took board
- [ ] tell redux-build/trilium-fork how tarball-host was solved

## Closed

- **tarball-host** (closed 2026-11-05): `dispatch-mode/parallel-uptime/board/tarball-host/README.md`
