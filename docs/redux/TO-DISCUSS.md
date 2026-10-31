# awm redux: to discuss

## Purpose & Contents

This doc is the stack of open threads for the redux interview. The interview runs depth first. It follows one thread to the bottom, writes the result into `DESIGN.md`, then pops the next thread.

- Push each new issue Tony raises onto the top of **Stack**, with its date and his words.
- Work the top thread first.
- For each thread, dispatch subagents to find the systems that already exist for it. Record the diff between what Tony described and what exists. Ask Tony about each diff whose implementation is unclear (Tony, 2026-10-30).
- Move a closed thread to **Closed**, with the `DESIGN.md` section that holds its result.
- Compact the interview session when a thread or a line of questioning closes (Tony, 2026-10-30).
- Put one question, or a small set of closely related questions, to Tony per message. Keep the rest in the thread's question queue (Tony, 2026-10-31, precedence `ebbb2807fff5986a`).

## Current thread

**1b. The session lifecycle.** How a session closes, and what happens when its result is not good. Tony: "sessions should close with a summary journal and if not good, a new session should be started rather than the old one with filled context being re started. compactions everytime would also be wasteful" (2026-10-30).

- Tony answered on 2026-10-30. `DESIGN.md` § Requirements R8 to R11 hold the result.
- Open: whether a session closes itself when it judges the work done, or waits for Tony to accept it.
- Diff of 1a against today (2026-10-30, read only):
  - R1 partial. The worker lifecycle brief (`cx/lifecycle.py:90-99`, precedence `909435672ebbd415`) tells a session to find or create its scope and move with `reflection cd`. Only Discord `/new-agent` tells it to rename itself, in the user prompt. Terminal sessions and pinned seats get no brief. The brief never mentions creating a project.
  - R2 partial. `start_other`, `cx` and plain `claude` all launch straight into a folder, and Bash `cd` works. `reflection cd` is not a single entry point.
  - R3 missing. No occupancy check exists. Live means "process running" today.
  - R4 exists: `scope create` with `new_project` and `clone_url` or `fork_url`. `start_other` cannot create a new project, and a cloned scope gets a new `feat/<scope>` branch.
  - R5 exists with gaps. Nothing deletes scopes automatically. The delete check misses ignored files, `dvc status` drift, unpushed commits and a live session.
  - R6 missing. `scope heal` is manual and only reports.
  - R7 mostly missing. `request_human` and card messages cover steps 1 and 4. No per-scope queue or release event exists.
- Diff of 1b against today (2026-10-30, read only):
  - R8 partial. The `retire` skill debriefs, reports to the starter, then runs `reflection stop`, which deletes the job. A close takes 2 to 4 writes: a journal post, a `request_human` review summary, and a message to the parent or a `social send`. Journal posts already copy onto the session's card.
  - R9 partial. Four ids exist: the 8-hex job id, the Claude session UUID, the card's Trilium id, and the journal post UUID. A terminal session has no short id. No button starts a task from a report. `start_other replaces` reads the old job's state, which retire deletes.
  - R10 conflicts. Retire removes the job, so `resume_other` refuses it. `replaces` seeds the new session from the last 8 transcript turns, not from the journal.
  - R11 mostly exists, but the rule today is "always deploy" (precedence `39821f68`). Nothing names work that does not deploy.
  - Today the agent decides it is done. A review summary followed by retire lands the card in Done (commit `46b01ace`). Only a Blocked card waits for Tony.

Question queue for 1b and the 1a follow-ups. Ask the top one. Strike it when Tony answers.

1. ~~1b: Who decides a session is done.~~ Answered 2026-10-31: R12.
2. ~~1b: What "resumable" keeps.~~ Answered 2026-10-31: the transcript only (R10).
3. 1b: Which id is the session hash. It must lead to the Claude session id, which a resume needs.
3b. 1b: Where the new-task-from-report button lives.
3c. 1b: Where a follow-up session starts: the old scope, or wherever it routes itself.
4. 1b: Which work skips the deploy.
5. 1a: Where a new session starts before it picks a scope, and how strict "enter only through `reflection cd`" is.
6. 1a: Whether Tony's own terminal sessions follow the brief and the occupancy check.
7. 1a: What counts as "live" for R3: the process runs, the card is Active, Blocked or Pending review, or either.
8. 1a: Which branch a cloned project's scope uses.
9. 1a: What blocks a scope delete, and what `force` may override.
10. 1a: What the hygiene check may do, and whether it triggers on a scope count, a time interval, or both.
11. 1a: The order of the queue for a busy scope, and what a waiting session does when Tony answers before the scope frees.

Parent thread 1 (end state) waits for Tony to confirm `DESIGN.md` § Goal. Its children are 1a to 1j.

## Stack

Top of the stack comes first.

1k. **Automatic dispatch of follow-up work.** Learn from Tony's recorded decisions to dispatch jobs for unforeseen issues on their own. Tony: "Ideally my decisions are recorded in precedent system so taht we can automate this (dyanamic responsive auto dispatch of jobs to address unforseen issues) in the future" (2026-10-31).

1c. **Dependencies between sessions.** A question that surfaces a larger issue spawns tasks. They report back to the asking session and resume it. Tony: "a sort of asyn model like promises" (2026-10-30).

1d. **The pending queue and the active list.** One interface of reports and blockers that Tony picks from, beside a list of active work. Tony: "I should be able to just click and start talking to the agent with the report or issue" and "The Kanban board attempts to do this, but is not reliable and awkward" (2026-10-30). This absorbs the parked board input.

1e. **Seats as sessions.** Seats, the Discord DM included, become sessions on a channel. Agents use subagents freely and create top-level sessions only with Tony's permission. A seat agent can help Tony work the pending queue (2026-10-30).

1f. **The three channels.** Chat with humans (social seats). Chat between agents of different systems that share no awm network, through private chat rooms over websocket or HTTPS polling. The "dumb" tether to machines. Tony: "It is really frustrating trying to get two way coms between 2 agentic systems" (2026-10-30).

1g. **Communication between awm nodes.** Tony: "This doesnt cover coms between nodes of awm itself which needs discussion. I think we need to drop foreign nodes in favour of a searchable endpoint instead" (2026-10-30).

1h. **Install and bootstrap.** A Rust conversion, true plugins, and Claude cloud agents that test fresh installs and adding nodes. This absorbs the parked gateway, plugin and bootstrap input (Tony, 2026-10-30).
   - Local throwaway environments that behave like cloud agents, to avoid their cost. Tony: "we should also consider settup up a system that can spawn throway temp environments that act the same" (2026-10-30).

1i. **The Armano box.** A new box for a client, for mass spec automation, set up soon with an agent's help (Tony, 2026-10-30).

1j. **IT services through the tether, and proof of ownership.** Agents debug boxes faster than Tony, but this can trigger security concerns. Tony: "We need ways to prove I own the box like the current permissions screen that requires me to be at the keyboard or have prexisting ssh access. Need to be careful here" (2026-10-30).

2. **Why awm exists.** What would Tony lose if awm vanished tomorrow?

3. **Who uses it.** Tony, lab members through seats, collaborators, the public. What can each do or reach, and what must each never touch?
   - Today: Tony, and lab members through Slack seats on altair, under his supervision (Tony, 2026-10-30).
   - To do: Tony gives his own view. Then reconcile it with the survey below. Tony: "reconciliation of the 2 perspectives will give the best answer" (2026-10-30).
   - Survey of current use (2026-10-30, read only). Users beyond Tony and the lab:
     - A second Arbutus admin approves admin windows by Slack reply.
     - A collaborator's swarm, peer `shaula`, holds read grants on journals, kb, notes, precedence and writing.
     - Bots arrive through Discord kept seats.
     - altair's HTTPS edge has 2 static users, not yet identified.
     - People who accept a tether session reach an agent from their own computers.
   - 3a. **The Slack seat system.** Review how seats work and discuss whether they keep their shape. Tony: "this is another system to review and discuss" (2026-10-30).
     - Shape today:
       - The `social` service and the seat config live on mira.
       - Slack seat agents run on altair. Discord seat agents run on deneb.
       - Kept seats in mira's `social.toml` start and replace the agents through cx.
       - One session, `slack-main`, holds Tony's DM and 7 member DMs. 5 members have their own agents.
     - Supervision is by exception: agents call `request_human` for costly, irreversible or permission questions (precedence `89e3306ab5f9656f`, `1f7107b1750152f3`).
     - Restrictions on a seat are prose only:
       - Seat agents run in mode `worker` with skip-permissions and the full MCP surface. Tony's personal accounts are reachable.
       - An unscoped `social history` returns every chat, Tony's DM included.
     - Drift:
       - mizar's copy of the `opal-seat-holder` skill is out of date.
       - Two lab agents run in scopes not tagged `lab`.
       - `slack-main` runs in the `awm/dev` worktree.
       - Editing a brief replaces the agent and loses its context.
     - Open for Tony:
       - Permanent home of social and seat agents, given the planned mira migration.
       - A real restricted mode for lab seats.
       - The canonical copy of the SOP.
       - Who the 2 static edge users are.
       - Whether seat scopes are private to foreign peers.
       - An allowlist for who can reach Opal, or anyone in the workspace.

4. **What made it foundational.** Which problem made Tony think "rebuild" rather than "patch"?

5. **Done.** What makes the redux finished?

## Parked inputs

Raised before the redux started. Each enters the stack when a thread reaches it.

- Unified permissions across boxes, Tailscale included, and the migration to it (Tony, 2026-10-29).
- A Slack-only box on Arbutus, and the compute model (Tony, 2026-10-29).
- Migrate mira onto mizar or alcor (Tony, 2026-10-30). mira hosts `social` and the seat config today.
- One always-on shared Chrome on alcor or mizar for agents, and for Tony to solve captchas (Tony, 2026-10-30).
- Three workstream sessions on altair (`c4455c36`, `bc32a23b`, `e75d93a3`) disappeared after 2026-10-30 03:07Z. Resume them or leave them stopped.
- The Claude Code daemon does not restart pinned jobs after a host reboot.

## Closed

- 1a. The work hierarchy and routing (2026-10-30). Result: `DESIGN.md` § Requirements, R1 to R7. Its follow-up questions sit in the 1b question queue.
