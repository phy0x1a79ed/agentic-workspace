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

## Current thread

**1b. The session lifecycle.** How a session closes, and what happens when its result is not good. Tony: "sessions should close with a summary journal and if not good, a new session should be started rather than the old one with filled context being re started. compactions everytime would also be wasteful" (2026-10-30).

- Results: `sessions.md`. The gaps from today are there too.

Question queue for 1b and the 1a follow-ups. Ask the top one. Strike it when Tony answers.

1. ~~1b: Who decides a session is done.~~ Answered 2026-10-31: R12.
2. ~~1b: What "resumable" keeps.~~ Answered 2026-10-31: the transcript only (R10).
3. ~~1b: Which id is the session hash.~~ Answered 2026-10-31: one platform-prefixed id (R9), and parent links (R13).
3b. 1b: Where the new-task-from-report button lives.
3c. 1b: Where a follow-up session starts: the old scope, or wherever it routes itself.
3d. 1l: Whether a citation can point at a session that has no journal yet, such as a live session the agent talked to.
4. 1b: Which work skips the deploy.
5. 1a: Where a new session starts before it picks a scope, and how strict "enter only through `reflection cd`" is.
6. 1a: Whether Tony's own terminal sessions follow the brief and the occupancy check.
7. 1a: What counts as "live" for R3: the process runs, the card is Active, Blocked or Pending review, or either.
8. 1a: Which branch a cloned project's scope uses.
9. 1a: What blocks a scope delete, and what `force` may override.
10. 1a: What the hygiene check may do, and whether it triggers on a scope count, a time interval, or both.
11. 1a: The order of the queue for a busy scope, and what a waiting session does when Tony answers before the scope frees.

Parent thread 1 (end state) waits for Tony to confirm `goal.md`. Its children are 1a to 1j.

## Stack

Top of the stack comes first.

1k. **Automatic dispatch of follow-up work.** Learn from Tony's recorded decisions to dispatch jobs for unforeseen issues on their own. Tony: "Ideally my decisions are recorded in precedent system so taht we can automate this (dyanamic responsive auto dispatch of jobs to address unforseen issues) in the future" (2026-10-31).

1c. **Dependencies between sessions.** A question that surfaces a larger issue spawns tasks. They report back to the asking session and resume it. Tony: "a sort of asyn model like promises" (2026-10-30).

1d. **The pending queue and the active list.** One interface of reports and blockers that Tony picks from, beside a list of active work. Tony: "I should be able to just click and start talking to the agent with the report or issue" and "The Kanban board attempts to do this, but is not reliable and awkward" (2026-10-30). This absorbs the parked board input.

1e. **Seats as sessions.** Seats, the Discord DM included, become sessions on a channel. Agents use subagents freely and create top-level sessions only with Tony's permission. A seat agent can help Tony work the pending queue (2026-10-30).

1f. **The three channels.** Chat with humans (social seats). Chat between agents of different systems that share no awm network, through private chat rooms over websocket or HTTPS polling. The "dumb" tether to machines. Tony: "It is really frustrating trying to get two way coms between 2 agentic systems" (2026-10-30).

1g. **Communication between awm nodes.** Tony: "This doesnt cover coms between nodes of awm itself which needs discussion. I think we need to drop foreign nodes in favour of a searchable endpoint instead" (2026-10-30).
   - Then: how `reflection` behaves across peers, once the network architecture settles (Tony, 2026-10-31).

1h. **Install and bootstrap.** A Rust conversion, true plugins, and Claude cloud agents that test fresh installs and adding nodes. This absorbs the parked gateway, plugin and bootstrap input (Tony, 2026-10-30).
   - Local throwaway environments that behave like cloud agents, to avoid their cost. Tony: "we should also consider settup up a system that can spawn throway temp environments that act the same" (2026-10-30).

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

## Parked inputs

Raised before the redux started. Each enters the stack when a thread reaches it.

- Unified permissions across boxes, Tailscale included, and the migration to it (Tony, 2026-10-29).
- A Slack-only box on Arbutus, and the compute model (Tony, 2026-10-29).
- Migrate mira onto mizar or alcor (Tony, 2026-10-30). mira hosts `social` and the seat config today.
- One always-on shared Chrome on alcor or mizar for agents, and for Tony to solve captchas (Tony, 2026-10-30).
- Three workstream sessions on altair (`c4455c36`, `bc32a23b`, `e75d93a3`) disappeared after 2026-10-30 03:07Z. Resume them or leave them stopped.
- The Claude Code daemon does not restart pinned jobs after a host reboot.

## Closed

- 1l. Idea lineage (2026-10-31). Result: `sessions.md` § Journals and lineage.
- 1a. The work hierarchy and routing (2026-10-30). Result: `scopes.md`. Its follow-up questions sit in the 1b question queue.
