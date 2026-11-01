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
- Go top down, depth first. Ask only the current thread's own questions. Move a question that depends on a thread not yet designed into that thread's queue (Tony, 2026-10-31, precedence `33bc8712feec7af7`).

## Current thread

**1c. Dependencies between sessions.** See the stack entry below for Tony's words.

Parent thread 1 (end state) waits for Tony to confirm `goal.md`. Its children are 1a to 1n.

## Stack

Top of the stack comes first.

1c. **Dependencies between sessions.** A question that surfaces a larger issue spawns tasks. They report back to the asking session and resume it. Tony: "a sort of asyn model like promises" and "The hard ones are cases where questions surface larger issues and I need to dispatch one or multiple tasks, which should then report back to the original asking session and resume it" (2026-10-30). Area doc: `sessions.md`. Question queue:
   1. Does `deploy` still return a ticket id at once (R26), now that a coordinator waits on the call itself?

1d. **The pending queue and the active list.** One interface of reports and blockers that Tony picks from, beside a list of active work. Tony: "I should be able to just click and start talking to the agent with the report or issue" and "The Kanban board attempts to do this, but is not reliable and awkward" (2026-10-30). This absorbs the parked board input.
   - Queued from 1b: where the new-task-from-report button lives (`sessions.md` § Lifecycle).
   - Queued from the 1a follow-ups: what a board card shows after its session ends (R42).

1e. **Seats as sessions.** Seats, the Discord DM included, become sessions on a channel. Agents use subagents freely and create top-level sessions only with Tony's permission. A seat agent can help Tony work the pending queue (2026-10-30).

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

## Parked inputs

Raised before the redux started. Each enters the stack when a thread reaches it.

- Unified permissions across boxes, Tailscale included, and the migration to it (Tony, 2026-10-29).
- A Slack-only box on Arbutus, and the compute model (Tony, 2026-10-29).
- Migrate mira onto mizar or alcor (Tony, 2026-10-30). mira hosts `social` and the seat config today.
- One always-on shared Chrome on alcor or mizar for agents, and for Tony to solve captchas (Tony, 2026-10-30).
- Three workstream sessions on altair (`c4455c36`, `bc32a23b`, `e75d93a3`) disappeared after 2026-10-30 03:07Z. Resume them or leave them stopped.
- The Claude Code daemon does not restart pinned jobs after a host reboot.

## Closed

- 1k. Automatic dispatch of follow-up work (2026-11-01). Result: `dispatch.md` R49 and R50, `routines.md` R47 and R48.
- 1a follow-ups. Liveness, cloned branches, deletes, hygiene and the busy-scope queue (2026-10-31). Result: `scopes.md` R42 to R46.
- 1m. Routines, continuous integration and deploys (2026-10-31). Result: `routines.md`.
- 1n. Timer-triggered dispatch (2026-10-31). Merged into 1m. Result: `routines.md` § Routines.
- 1b. The session lifecycle (2026-10-31). Result: `sessions.md`.
- 1l. Idea lineage (2026-10-31). Result: `sessions.md` § Journals and lineage.
- 1a. The work hierarchy and routing (2026-10-30). Result: `scopes.md`. Its follow-up questions are the current thread.
