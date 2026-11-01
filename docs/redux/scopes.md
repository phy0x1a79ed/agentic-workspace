# Scopes

## Purpose & Contents

This doc holds how a session finds its project and scope, enters it, shares it with other sessions, and how a scope is removed. Thread 1a produced it.

## Requirements

- **R1.** A new session routes itself. Tony may name a scope, but the session picks the scope and enters it. It creates the scope, or the project, when none fits. Its system prompt tells it to name itself and find its home first.
- **R2.** A session enters a scope only through `reflection cd`. The Claude Code harness blocks `cd` in some cases. The single entry point also lets awm enforce R3.
  - Only `reflection cd` changes the scope a session occupies. R3 reads that record.
  - A plain `cd` to read another folder stays allowed.
  - Instructions carry the rule, with no hook to enforce it. A hook is the next step if sessions break it. Tony agreed on 2026-10-31.
- **R40.** A new session starts in a lobby. The lobby is one fixed folder per node, outside every git worktree. A session in the lobby occupies no scope and cannot commit. It reads its brief, routes itself (R1, R18), then calls `reflection cd`. Tony: "agree with your cd suggestions and lobby" (2026-10-31).
- **R41.** Tony's own terminal sessions count for R3. Tony agreed on 2026-10-31.
  - A terminal session holds its scope like any live session. awm starts no agent into that scope.
  - Tony opening a scope that an agent holds gets a warning. He is not blocked or queued.
  - A terminal opened inside a scope folder skips routing, because Tony chose the scope. One opened in the lobby, or outside every scope, gets the brief and routes itself (R1, R40).
- **R3.** A scope is one git worktree and holds at most one live session. A session is live while it works, while it is blocked on a question, and while it waits for review.
- **R42.** An active agent session holds the claim on a scope. No card or other record holds it. The claim ends when the session ends. Tony: "if you are asking about what actually holds a scope as claimed, it is an active agent session. the claim dies with teh session" (2026-10-31).
  - What the board card shows after its session ends belongs to thread 1d.
- **R4.** An agent may create a project, which is a git repo. Example: "try out this new tool `<github link>`" clones the tool as a project and enters a branch as a scope.
- **R43.** A scope in a cloned project uses a new `feat/<scope>` branch from the upstream default branch. The upstream default branch stays clean for pulls. The project gets `dev` and `release` branches only when it gets a deploy routine (R24). Tony: "sounds reasonable" (2026-10-31).
- **R5.** A scope outlives its sessions. It stays until a session removes it. awm never deletes a scope when a session ends, because the worktree can hold DVC pins and uncommitted files. Tony: "At least Im not organized enough yet to allow this automation."
- **R44.** These conditions block a scope delete. Tony: "ok" (2026-10-31).
  1. A live session holds the claim (R42). `force` never overrides this.
  2. The worktree has uncommitted or untracked files. `force` overrides this.
  3. The worktree has ignored files outside `.awm/`. `force` overrides this.
  4. The branch has commits that no other branch or remote holds. `force` overrides this.
  - A forced delete lists everything it discarded.
- **R6.** A hygiene check finds scopes not in use and cleans them up. It triggers infrequently, for example every 10 new scopes, and acts only when action is needed. It is not an agent that churns on each new scope. Tony does not have to remember to run it.
- **R7.** A new session can need a scope that a live session occupies. When the scope clearly fits its task, the new session acts as follows:
  1. It asks Tony on the pending queue.
  2. It waits in a queue for the scope.
  3. awm notifies it when its turn comes. It enters the scope.
  4. It appends to its question that the conflict is now clear. It never withdraws the question. Tony: "these events may cause me to reconsider the scope layout and ask for new scopes."

## Gap from today

Surveyed read only on 2026-10-30.

- **R1 partial.** The worker lifecycle brief (`cx/lifecycle.py:90-99`, precedence `909435672ebbd415`) tells a session to find or create its scope and move with `reflection cd`. Only Discord `/new-agent` tells it to rename itself, in the user prompt. Terminal sessions and pinned seats get no brief. The brief never mentions creating a project.
- **R40 missing.** Every launcher starts a session inside a folder. Discord `/new-agent` always starts in `awm/dev` (`reporting/config.py:116`), so each new agent occupies that scope before it routes.
- **R41 missing.** A plain `claude` or `cx` session gets no brief and counts for no occupancy check.
- **R2 partial.** `start_other`, `cx` and plain `claude` all launch straight into a folder, and Bash `cd` works. `reflection cd` is not a single entry point.
- **R3 missing.** No occupancy check exists. Live means "process running" today.
- **R42 missing.** No claim record exists, so nothing releases a claim when a session ends.
- **R4 exists.** `scope create` takes `new_project` with `clone_url` or `fork_url`. `start_other` cannot create a new project.
- **R43 exists.** A cloned scope gets a new `feat/<scope>` branch from the detected default branch (`scopes.py:939`). The clone also adds a worktree on the default branch as a scope of its own (`projects.py:150`).
- **R5 exists.** Nothing deletes scopes automatically.
- **R44 partial.** `scope delete` blocks on uncommitted and untracked files, and `force` discards them (`scopes.py:286`). It refuses a folder that holds nested scopes. It keeps the branch unless `delete_branch` is set, and reports the restore commit. It misses ignored files, unique branch commits and a live session. A forced delete reports only a count and a sample.
- **R6 missing.** `scope heal` is manual and only reports.
- **R7 mostly missing.** `request_human` and card messages cover steps 1 and 4. No per-scope queue or release event exists.
