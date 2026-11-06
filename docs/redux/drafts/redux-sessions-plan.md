# redux-sessions plan

## Context

The redux build splits the design into lanes. This lane builds the work hierarchy and the session lifecycle: task ids that span sessions, scope claims and the busy-scope queue, safer scope deletes, the hygiene check, journal ids and citations, the start brief, and the skill drafts. Everything stays runnable under today's Python gateway, on branch `feat/redux-sessions`, committed locally and not deployed.

## What you said

> Your lane: the work hierarchy and session lifecycle. Read sessions.md, scopes.md, dispatch.md, parallel-work.md and drafts/parallel-work/SKILL.md, and the gap inventory rows for each.

> You own every requirement in those docs except sessions.md R10 and R51 (retire and wait), which redux-harness (job b7312951) owns; agree their interface with it.

> The card and task queue itself is redux-queue's (fleet.md R106). Agree the task id interface with it. The plugin contract comes from redux-core (job a92d02bf). Keep everything runnable under today's Python gateway until the Rust gateway exists.

> Local updates only, bring the peers up to date in one sweep when we are done

> just pause if unsure

## Issues

- I1. No task id spans sessions. The reporting ledger mints one integer id per session, and no verb joins a session to another task (S14, S2).
- I2. Nothing records which session holds a scope, so two sessions can work in one worktree (K5, K6, P3).
- I3. A session that needs a busy scope has no queue to wait in and no event when the scope frees (K12).
- I4. `scope delete` misses ignored files, unpushed commits and a live session, and a forced delete lists only a sample of what it discarded (K10).
- I5. Nothing finds or cleans unused scopes (K11).
- I6. The journal stamps the raw Claude UUID, drops a caller's session id, has no citation field and records no reads (S6, S7, S8).
- I7. `cx start` needs a fixed scope, starts every session inside a worktree, takes no task id, and its brief says nothing about naming, top-level starts, subagents or projects (S9, K1, K2, K16, K18, X4, S13).
- I8. The lineage graph exists only as scattered records: cx keeps parents and nothing keeps citations (R13, R16).
- I9. The retire skill takes 2 to 4 writes to close, and the parallel-work skill and its pointer are not packaged for install (S1, P1, P2).
- I10. A seat's down alarm fires when the checking box itself lost its connection (S17).

## High-level goals

- G1. One task id follows a piece of work across every session that works on it.
- G2. A scope has at most one live session, and a session that needs a busy scope waits its turn.
- G3. Removing a scope never loses work silently, and unused scopes get cleaned up without Tony remembering.
- G4. Journals say exactly which session wrote them and which journals they built on.
- G5. A new session starts outside every scope, routes itself, and knows the rules for naming, starting others and closing.
- G6. The skills that close and coordinate sessions are ready to install the night `reflection wait` ships.
- G7. Seat alarms are true alarms.

## Acceptance criteria

- `mint_task_id()` gives 8 URL-safe base64 characters, and the bytes of `00112233-4455-6677-8899-aabbccddeeff` give `ABEiM0RV`, the vector shared with redux-queue.
- `operations` serves `task_open`, `task_attach`, `task_join`, `task_for_session` and `task_sessions` with the signatures agreed with redux-queue. `task_join` returns `{task_id, main_session_id, folded_task_id}` and sends the main session a notice.
- A new session tracked by the ledger gets a task id with no call from the agent (R57 step 1).
- `scope claim` refuses a second live session with the holder named, queues it when asked, and frees the claim when the holder's process ends. `scope release` hands the scope to the head of the queue and notifies it.
- `scope delete` refuses on a live claim even with `force`. It refuses on ignored files outside `.awm/` and on commits that no other ref holds unless `force` is set. A forced delete lists every discarded path and commit.
- Every 10th `scope create` runs the hygiene check. It removes only clean, merged, idle worktrees, keeps their branches, and posts one report.
- A journal post carries `meta.session` in R9 form, accepts a caller's `session`, accepts `cites` that point only at journal posts, and `journal fetch` records each post read by a session.
- `cx start` accepts `task_id`, starts in the lobby when no scope is given, and records a scope given with `suggest` as a suggestion. The brief carries the R1, R50, R56, R57 and subagent rules.
- The tests of every touched dist pass: scopes, journal, reporting, cx and social.

## Tasks

- T1. Write and commit this plan (all).
- T2. Build the task id minter and the ledger-backed task store (G1).
- T3. Build scope claims and the busy-scope queue (G2).
- T4. Harden scope delete (G3).
- T5. Build the scope hygiene check (G3).
- T6. Commit a checkpoint and compact (all).
- T7. Stamp, cite and record journal reads (G4).
- T8. Rework the start: lobby, suggested scope, task id and brief (G5, G1).
- T9. Serve the lineage graph (G4).
- T10. Draft the retire, parallel-work and seat skills (G6).
- T11. Commit a checkpoint and compact (all).
- T12. Gate seat alarms on the box's own connection (G7).
- T13. Run every touched suite and commit (all).
- T14. Send the interface notes to the sibling lanes and the coordinator (all).
- T15. Debrief.

## Approach by task

### T1. Write and commit this plan

Write this file and commit it on `feat/redux-sessions`. Copy the gap inventory into `.awm/` for reference.

Gotchas: the area docs under `docs/redux/` belong to the interview session. Only `drafts/` takes edits from this lane.

### T2. Build the task id minter and the ledger-backed task store

Add a pure module to reporting that mints and checks task ids. Add two ledger tables, one mapping a task id to its title and ledger row, and one listing each session of a task with its role, node and state. Expose the five agreed verbs on `operations`, hidden from the MCP surface for agents except `task_join` and `task_for_session`. Mint a task id inside `Ledger.ensure` when it makes a new row, so every tracked session gets one. `task_join` folds the joiner's own task into the target and calls `reflection message` through the gateway to tell the main session. redux-queue swaps this backend for its alcor queue later, with the same verb names. reflection `whoami` imports `task_for_session` lazily (agreed with redux-harness).

Gotchas: a schema bump must migrate the live ledger in place. The ledger keys sessions by raw Claude UUID today, so the lookup must accept both forms until harness's R9 module lands. Folding must not delete a row, because the ledger never deletes.

### T3. Build scope claims and the busy-scope queue

Add a claims table and a queue table to the scopes DB. `scope claim {project, scope, session_id, pid, queue?, terminal?}` records the holder with its pid and process start time. A claim counts as live while that process runs with the same start time, so the claim dies with the session (R42). A terminal claim on a held scope warns and records no claim (R41). A queued request gets its first-come position (R46). `scope release` ends the claim and notifies the queue head through `reflection message` over the gateway. `scope claims` lists holders and queues. reflection `cd` calls claim and release (redux-harness builds that call).

Gotchas: the gateway stamps `_caller_pid` only for MCP callers, so the verb must take an explicit pid from reflection. A stale claim must free itself on read, not wait for a sweep. A session in `reflection wait` has no process, so whether it still holds its scope is open (see Still unsure).

### T4. Harden scope delete

Extend the worktree guard with the R44 checks: a live claim that `force` never overrides, ignored files outside `.awm/`, and branch commits that no other branch or remote ref holds. Return the full list of discarded paths and commits on a forced delete.

Gotchas: `git status --ignored` lists data hardlinks under `data/` as ignored files when DVC tracks them. Exclude paths that a tracked `.dvc` pin covers, or every DVC scope becomes undeletable. A nested scope folder must keep its existing refusal.

### T5. Build the scope hygiene check

Count scope creates in the scopes DB. Every 10th create runs the check after the create returns. The check removes a worktree only when T4's guard passes without `force`, the branch is merged into the project's `release`, `dev` or upstream default branch, and no claim touched the scope for 14 days. It keeps the branch and posts one report through `operations ops_post` with kind `scope-hygiene`, listing each held scope and why.

Gotchas: run the check off the create's request path so a slow sweep cannot time out a create. Idle time falls back to the newest of the last claim, the last commit and the `.awm/` mtime, because claims are new.

### T6. Commit a checkpoint and compact

Commit T2 to T5 with their tests. Journal the phase. Compact with `reflection compact`, follow-up "continue with T7".

Handoff: the plan file is this doc. The todo list holds T1 to T15. The interfaces agreed so far: harness owns everything under `awm/services/reflection/`, `cch/` and `dsh/`, ships `reflection/ids.py` with `r9` and `parse_r9` on `feat/redux-harness`, and calls `scope claim` and `scope release` from `cd`. redux-queue takes the five task verbs as named and ships its queue at `awm/plugins/taskqueue/` on `feat/redux-queue`. New core plugins go at `awm/plugins/<name>/` (redux-core, contract at `redux-core/docs/redux/drafts/plugin-contract.md`).

### T7. Stamp, cite and record journal reads

Change `_with_session` so the stamp is the R9 form and a caller-supplied `session` is kept (R17). Add `meta.cites`, a list of `{journal, reason}`. Validate that each cited id is a journal post. Record each post that `journal fetch` returns to a session in a reads table, and serve them as the candidate list with a new `journal reads {session}` verb (R15).

Gotchas: a fetch can return 50 posts, so batch the read inserts. Foreign callers have no session and record no reads. The R9 helper comes from harness's branch. Until it merges, use a local fallback with the same output.

### T8. Rework the start: lobby, suggested scope, task id and brief

Teach `cx start` three things. With no scope, it starts the session in a lobby folder outside every worktree (R40). With `suggest:true`, the scope is only a suggestion, and the brief says the session may choose another (R18). With `task_id`, it records the task id in the lineage and attaches the new session to that task (R57 step 3). Rewrite the brief to cover routing and project creation (R1), names as `<task>/<descriptor>` (R56), when to start top-level sessions (R50), subagents and harness worktrees (K16, K18), and closing (R11, R12). Point Discord `/new-agent` at the lobby (K2).

Gotchas: the lobby must not sit inside the workspace root, which is itself a git checkout. A session in the lobby must not trip the trust check that `cx start` runs on its folder. Seats keep their own brief.

### T9. Serve the lineage graph

Add `operations lineage {session_id}` that returns the hard edges, from cx lineage records and ledger parent links, and the soft edges, from journal citations. It reads both through the gateway.

Gotchas: R13 says a clear gives a new id with the old as parent. Confirm that `hook_session_clear` records that link, and add it there if not.

### T10. Draft the retire, parallel-work and seat skills

Write `drafts/retire/SKILL.md`: the debrief journal is the one report (R8), the session closes on its own (R12), and its last step is `reflection wait` when it waits, else `reflection stop`. Package the approved parallel-work draft unchanged with its `CLAUDE.md` pointer text and an install script that runs only when `reflection wait` exists on the node. Draft the `opal-seat-holder` change for R67: each real request becomes one task session, not a pinned per-person agent.

Gotchas: nothing under `~/.claude/` changes tonight. Installing a skill is part of the one-sweep deploy.

### T11. Commit a checkpoint and compact

Commit T7 to T10. Journal the phase. Compact with follow-up "continue with T12".

Handoff: list the commits, the open questions sent to the coordinator, and any interface reply still owed.

### T12. Gate seat alarms on the box's own connection

In the social keeper, test the checking box's own internet connection before it opens an outage. A box with no connection raises no seat alarm (R78). Add a `local_only` keeper mode that keeps only the seats of its own node, off by default, so each box can check its own seats once social runs per node.

Gotchas: the keeper-off-home fix (witness probe) shipped on 2026-11-01. Compose with it, do not undo it. Social's placement belongs to redux-trust (users.md R129).

### T13. Run every touched suite and commit

Run `awm/gateway/scripts/run-tests.sh scopes journal reporting cx social`. Fix what this lane broke. Record any failure that already fails on the base commit.

Gotchas: social has Slack tests that fail on mizar for want of `slack_sdk`. Compare with the baseline run.

### T14. Send the interface notes to the sibling lanes and the coordinator

Send redux-harness the `scope claim` schema and the `task_for_session` path. Send redux-queue the shim's verbs and record shape. Send the coordinator one line with the branch and the open questions.

Gotchas: send each note once, after the commit it describes.

### T15. Debrief

Run the `debrief` skill. Journal the session to `awm/redux-sessions`.

## Still unsure about

- Does a session in `reflection wait` keep its scope claim? The default built here: yes, while harness's wait ledger lists it. Sent to the coordinator.

## Callouts

- Today's Trilium board keeps one card per ledger row. The shim records which sessions share a task, but folding cards on the board is left to redux-queue's new board, which renders `task_sessions`.
- R41's terminal hook and R2's single entry point land through redux-harness's `cd` and hooks, not here.
- R68 needs redux-queue's passkey panel (`queue.md` R65). This lane builds nothing for it.
- R49 is a given and needs no code.
