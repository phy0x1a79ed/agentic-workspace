# Scopes

## Purpose & Contents

This doc holds how a session finds its project and scope, enters it, shares it with other sessions, and how a scope is removed. Threads 1a and 1o produce it.

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
- **R3.** A scope is one git worktree and holds at most one live session. A session is live while it works, while it is blocked on a question, while it waits for review, and while it waits on other sessions (`sessions.md` R51). A waiting session keeps its claim on the scope. Tony: "Waiting sessions  hold scope" (2026-11-03).
- **R42.** An active agent session holds the claim on a scope. No card or other record holds it. The claim ends when the session ends. Tony: "if you are asking about what actually holds a scope as claimed, it is an active agent session. the claim dies with teh session" (2026-10-31).
  - A card's info panel shows each of its sessions as active or inactive (`queue.md` R62).
- **R4.** An agent may create a project, which is a git repo. Example: "try out this new tool `<github link>`" clones the tool as a project and enters a branch as a scope.
- **R43.** A scope in a cloned project uses a new `feat/<scope>` branch from the upstream default branch. The upstream default branch stays clean for pulls. The project gets `dev` and `release` branches only when it gets a deploy routine (R24). Tony: "sounds reasonable" (2026-10-31).
- **R5.** A scope outlives its sessions. It stays until a session removes it. awm never deletes a scope when a session ends, because the worktree can hold DVC pins and uncommitted files. Tony: "At least Im not organized enough yet to allow this automation."
- **R44.** These conditions block a scope delete. Tony: "ok" (2026-10-31).
  1. A live session holds the claim (R42). `force` never overrides this.
  2. The worktree has uncommitted or untracked files. `force` overrides this.
  3. The worktree has ignored files outside `.awm/`. `force` overrides this.
  4. The branch has commits that no other branch or remote holds. This blocks even when the delete keeps the branch. `force` overrides this. Tony: "always, for unmerged commits or dirty files" (2026-11-03).
  - A forced delete lists everything it discarded.
- **R6.** A hygiene check finds scopes not in use and cleans them up. It triggers infrequently, for example every 10 new scopes, and acts only when action is needed. It is not an agent that churns on each new scope. Tony does not have to remember to run it.
- **R45.** The hygiene check (R6) runs after every 10 new scopes. Tony: "lets keep scope heal manual. otherwise the 10 scopes hygiene stays as proposed" (2026-10-31).
  - It removes a scope's worktree only when R44 needs no `force`, the branch is merged, and no session used the scope for an idle period such as 14 days. It keeps the branch so the scope can be restored.
  - It deletes no other scope. It puts one report on the pending queue that lists each held scope and why.
  - `scope heal`, the repair of scope files, stays a manual command apart from the hygiene check.
  - The count of new scopes is its only trigger. No timer runs it. Tony: "R45 only after 10, not time based" (2026-10-31).
- **R7.** A new session can need a scope that a live session occupies. When the scope clearly fits its task, the new session acts as follows:
  1. It asks Tony on the pending queue.
  2. It waits in a queue for the scope.
  3. awm notifies it when its turn comes. It enters the scope.
  4. It appends to its question that the conflict is now clear. It never withdraws the question. Tony: "these events may cause me to reconsider the scope layout and ask for new scopes."
- **R46.** The queue for a busy scope (R7) is first come, first served, by the time each session asked. Tony: "yes to both" (2026-10-31).
  - A waiting session follows Tony's answer when it arrives before the scope frees.
  - Sent to another scope, it leaves the queue and moves there.
  - Told to wait, it keeps its place.
  - With its task cancelled, it leaves the queue and ends.

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
- **R6 missing.** No hygiene check exists. `scope heal` repairs scope files, such as the `AGENTS.md` import, context links and the data link, and reports missing worktrees. It never looks for unused scopes.
- **R7 mostly missing.** `request_human` and card messages cover steps 1 and 4. No per-scope queue (R46) or release event exists.

Surveyed read only on 2026-11-01 for thread 1o, subagents.

- **awm cannot see a subagent.** A Claude Code subagent runs inside its parent's process. awm names the caller by its Claude Code process (`resolve_caller_pid`, `gateway/mcp_caller.py`), so every awm call that a subagent makes counts as its parent's. awm sees a subagent only as a transcript file in the parent's `subagents/` folder, which the transcripts sweep archives (`transcripts/layout.py`).
- **A subagent cannot enter a scope on its own.** `reflection cd` types `/cd` into the calling session (`reflection/control.py`, `cd`). Called from a subagent, it moves the parent. A test on 2026-11-01 showed that a subagent's `cd` lasts for one Bash call only. The next call starts again in the parent's folder. A subagent works in another scope only through a `cd` at the start of each command, absolute paths or `git -C`. The subagent also carries its parent's Claude session id (`CLAUDE_CODE_SESSION_ID`).
- **No tool sets a subagent's folder to an awm scope.** The Agent tool takes no folder. Its `isolation: "worktree"` makes a new worktree under `.claude/worktrees/` on a new branch. Per the tool's own description, `EnterWorktree` called from a subagent with a pinned folder accepts only a worktree under `.claude/worktrees/` of the same repository. awm scopes live at `projects/<project>/<scope>/`. Untested.
- **Harness worktrees conflict with the brief.** The Agent tool's `isolation: "worktree"` and the `EnterWorktree` tool make a git worktree under `.claude/worktrees/` on a new branch. The lifecycle brief forbids worktrees there (`BRIEF`, `cx/lifecycle.py`). The Claude Code prompt for a background job tells the job to call `EnterWorktree` before its first edit.
- **The harness tracks subagents, awm does not.** A session lists its own subagents with `ListAgents` and continues one with `SendMessage`. No awm record holds a subagent, its scope or its branch.
- **The lifecycle brief does not mention subagents.** It tells a started session to find or create its scope and to move there with `reflection cd`.
