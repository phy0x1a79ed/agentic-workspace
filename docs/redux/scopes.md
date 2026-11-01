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
- **R4.** An agent may create a project, which is a git repo. Example: "try out this new tool `<github link>`" clones the tool as a project and enters a branch as a scope.
- **R5.** A scope outlives its sessions. It stays until a session removes it. awm never deletes a scope when a session ends, because the worktree can hold DVC pins and uncommitted files. Tony: "At least Im not organized enough yet to allow this automation."
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
- **R4 exists.** `scope create` takes `new_project` with `clone_url` or `fork_url`. `start_other` cannot create a new project, and a cloned scope gets a new `feat/<scope>` branch.
- **R5 exists with gaps.** Nothing deletes scopes automatically. The delete check misses ignored files, `dvc status` drift, unpushed commits and a live session.
- **R6 missing.** `scope heal` is manual and only reports.
- **R7 mostly missing.** `request_human` and card messages cover steps 1 and 4. No per-scope queue or release event exists.
