# awm redux: to discuss

## Purpose & Contents

This doc is the stack of open threads for the redux interview. The interview runs depth first. It follows one thread to the bottom, writes the result into the area doc that `README.md` names, then pops the next thread.

- Push each new issue Tony raises onto the top of **Stack**, with its date and his words.
- Work the top thread first.
- For each thread, dispatch subagents to find the systems that already exist for it. Record the diff between what Tony described and what exists. Ask Tony about each diff whose implementation is unclear (Tony, 2026-10-30).
- Record each diff in the area doc's **Gap from today** section.
- Move a closed thread to **Closed**, with the area doc that holds its result.
- Compact the interview session when a thread or a line of questioning closes (Tony, 2026-10-30).
- Put one question, or a small set of closely related questions, to Tony per message. Keep the rest in the thread's question queue (Tony, 2026-10-31, precedence `ebbb2807fff5986a`).
- Keep the design simple. Add no feature until a path needs it (Tony, 2026-11-01).
- Commit each change locally only. Push the branch and write precedence on the peers in one sweep when the interview is done. Hold each precedence change in **Pending sweep** until then. Tony: "Local updates only, bring the peers up to date in one sweep when we are done" (2026-11-01).
- Go top down, depth first. Ask only the current thread's own questions. Move a question that depends on a thread not yet designed into that thread's queue (Tony, 2026-10-31, precedence `33bc8712feec7af7`).

## Current thread

**1d. The pending queue and the active list.** See the stack entry below for Tony's words. The survey and its gap are in `queue.md`.

Parent thread 1 (end state) waits for Tony to confirm `goal.md`. Its children are 1a to 1q.

## Stack

Top of the stack comes first.

1d. **The pending queue and the active list.** One interface of reports and blockers that Tony picks from, beside a list of active work. Tony: "I should be able to just click and start talking to the agent with the report or issue" and "The Kanban board attempts to do this, but is not reliable and awkward" (2026-10-30). This absorbs the parked board input.
   - Queued from 1b: where the new-task-from-report button lives (`sessions.md` § Lifecycle).
   - Queued from the 1a follow-ups: what a board card shows after its session ends (R42).

1e. **Seats as sessions.** Seats, the Discord DM included, become sessions on a channel. Agents use subagents freely and start top-level sessions as `dispatch.md` R50 allows. A seat agent can help Tony work the pending queue (2026-10-30).

1f. **The three channels.** Chat with humans (social seats). Chat between agents of different systems that share no awm network, through private chat rooms over websocket or HTTPS polling. The "dumb" tether to machines. Tony: "It is really frustrating trying to get two way coms between 2 agentic systems" (2026-10-30).

1g. **Communication between awm nodes.** Tony: "This doesnt cover coms between nodes of awm itself which needs discussion. I think we need to drop foreign nodes in favour of a searchable endpoint instead" (2026-10-30).
   - Then: how `reflection` behaves across peers, once the network architecture settles (Tony, 2026-10-31).

1h. **Install and bootstrap.** A Rust conversion, true plugins, and Claude cloud agents that test fresh installs and adding nodes. This absorbs the parked gateway, plugin and bootstrap input (Tony, 2026-10-30).
   - Local throwaway environments that behave like cloud agents, to avoid their cost. Tony: "we should also consider settup up a system that can spawn throway temp environments that act the same" (2026-10-30).
   - Plugins as their own projects, each with its own refresh hook, migrated out of the awm repo (Tony, 2026-10-31, R33).

1i. **The Armano box.** A new box for a client, for mass spec automation, set up soon with an agent's help (Tony, 2026-10-30).

1j. **IT services through the tether, and proof of ownership.** Agents debug boxes faster than Tony, but this can trigger security concerns. Tony: "We need ways to prove I own the box like the current permissions screen that requires me to be at the keyboard or have prexisting ssh access. Need to be careful here" (2026-10-30).

2. **Why awm exists.** What would Tony lose if awm vanished tomorrow?

3. **Who uses it.** Tony, lab members through seats, collaborators, the public. What can each do or reach, and what must each never touch?
   - Today: Tony, and lab members through Slack seats on altair, under his supervision (Tony, 2026-10-30).
   - To do: Tony gives his own view. Then reconcile it with the survey in `users.md`. Tony: "reconciliation of the 2 perspectives will give the best answer" (2026-10-30).
   - The survey of today's users and seats is in `users.md`.
   - 3a. **The Slack seat system.** Review how seats work and discuss whether they keep their shape. Tony: "this is another system to review and discuss" (2026-10-30). Open for Tony:
     - Permanent home of social and seat agents, given the planned mira migration.
     - A real restricted mode for lab seats.
     - The canonical copy of the SOP.
     - Who the 2 static edge users are.
     - Whether seat scopes are private to foreign peers.
     - An allowlist for who can reach Opal, or anyone in the workspace.

4. **What made it foundational.** Which problem made Tony think "rebuild" rather than "patch"?

5. **Done.** What makes the redux finished?

6. **Review the protocol docs.** Add the deploy to the core awm docs, then review every protocol doc. It runs after the other threads settle. Tony: "we should also add the deploy to the core awm docs -> I guess we need to review all protocol docs once we settle everything this session." (2026-11-01).

## Pending sweep

Peer writes held until the end of the interview.

- Precedence `f7def9a0d79ac72f`: set the decision to "A task name is a slug of 2 or 3 words (e.g. redux-naming). The coordinator takes the task name. Each session it starts is named <task>/<descriptor>, with a descriptor of 1 to 3 words for its concern (e.g. redux-naming/survey). Keep names under about 40 characters as a guideline. Names serve legibility only. awm tracks sessions by the R9 session id (2-character platform code plus the Claude session id) and tasks by a task id that awm mints for each card: the first 8 characters of a random UUID in URL-safe base64. Tony: "Id  prefer human readable prefix and session descriptor but fear will get long", "lets omit the id in the name. Names can be for legibility only." and "The real tracking is the 2 character claude indicator plus claude session id for sessions and the task ids minted by awm" (2026-11-01)."

## Parked inputs

Raised before the redux started. Each enters the stack when a thread reaches it.

- Unified permissions across boxes, Tailscale included, and the migration to it (Tony, 2026-10-29).
- A Slack-only box on Arbutus, and the compute model (Tony, 2026-10-29).
- Migrate mira onto mizar or alcor (Tony, 2026-10-30). mira hosts `social` and the seat config today.
- One always-on shared Chrome on alcor or mizar for agents, and for Tony to solve captchas (Tony, 2026-10-30).
- Three workstream sessions on altair (`c4455c36`, `bc32a23b`, `e75d93a3`) disappeared after 2026-10-30 03:07Z. Resume them or leave them stopped.
- The Claude Code daemon does not restart pinned jobs after a host reboot.

## Closed

- 1p. The parallel work skill (2026-11-01). Result: `parallel-work.md` R59 and the approved draft `drafts/parallel-work/SKILL.md`. The question of where subagent work ends was not needed. `dispatch.md` R50 covers it.
- 1q. One card tracks several sessions (2026-11-01). Result: `sessions.md` R9, R56, R57 and R58. awm mints a task id for each card, and sessions join a task by that id. Names serve legibility only.
- 1o. Subagents and scopes (2026-11-01). Result: `dispatch.md` R50, `sessions.md` R11, R52 path 2 and R56. Coordinated work lands through the coordinator's one `deploy` per project.
- 1c. Dependencies between sessions (2026-11-01). Result: `sessions.md` R51 and R52, `routines.md` R26.
- 1k. Automatic dispatch of follow-up work (2026-11-01). Result: `dispatch.md` R49 and R50, `routines.md` R47 and R48.
- 1a follow-ups. Liveness, cloned branches, deletes, hygiene and the busy-scope queue (2026-10-31). Result: `scopes.md` R42 to R46.
- 1m. Routines, continuous integration and deploys (2026-10-31). Result: `routines.md`.
- 1n. Timer-triggered dispatch (2026-10-31). Merged into 1m. Result: `routines.md` § Routines.
- 1b. The session lifecycle (2026-10-31). Result: `sessions.md`.
- 1l. Idea lineage (2026-10-31). Result: `sessions.md` § Journals and lineage.
- 1a. The work hierarchy and routing (2026-10-30). Result: `scopes.md`. Its follow-up questions are the current thread.
