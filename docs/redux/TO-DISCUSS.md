# awm redux: to discuss

## Purpose & Contents

This doc is the stack of open threads for the redux interview. The interview runs depth first. It follows one thread to the bottom, writes the result into `DESIGN.md`, then pops the next thread.

- Push each new issue Tony raises onto the top of **Stack**, with its date and his words.
- Work the top thread first.
- Move a closed thread to **Closed**, with the `DESIGN.md` section that holds its result.

## Current thread

**1. End state.** A year out, what does Tony do with the fleet on an ordinary day, and what does it do without him?

- Tony answered on 2026-10-30. `DESIGN.md` § Goal holds the draft. It waits for his confirmation.
- His answer raised the sub-threads 1a to 1j below. Each one is a child of thread 1.

## Stack

Top of the stack comes first.

1a. **The work hierarchy and routing.** How a task Tony states finds its project and scope, and when a new scope or a duplicate scope is made. Tony: "I'd like the agent to have enough context to create or route the task to a scope" and "if a scope is already active with a session, the correct thing to do would actually be to duplicate it and start another session rather than append work to an ongoing session" (2026-10-30).

1b. **The session lifecycle.** How a session closes, and what happens when its result is not good. Tony: "sessions should close with a summary journal and if not good, a new session should be started rather than the old one with filled context being re started. compactions everytime would also be wasteful" (2026-10-30).

1c. **Dependencies between sessions.** A question that surfaces a larger issue spawns tasks. They report back to the asking session and resume it. Tony: "a sort of asyn model like promises" (2026-10-30).

1d. **The pending queue and the active list.** One interface of reports and blockers that Tony picks from, beside a list of active work. Tony: "I should be able to just click and start talking to the agent with the report or issue" and "The Kanban board attempts to do this, but is not reliable and awkward" (2026-10-30). This absorbs the parked board input.

1e. **Seats as sessions.** Seats, the Discord DM included, become sessions on a channel. Agents use subagents freely and create top-level sessions only with Tony's permission. A seat agent can help Tony work the pending queue (2026-10-30).

1f. **The three channels.** Chat with humans (social seats). Chat between agents of different systems that share no awm network, through private chat rooms over websocket or HTTPS polling. The "dumb" tether to machines. Tony: "It is really frustrating trying to get two way coms between 2 agentic systems" (2026-10-30).

1g. **Communication between awm nodes.** Tony: "This doesnt cover coms between nodes of awm itself which needs discussion. I think we need to drop foreign nodes in favour of a searchable endpoint instead" (2026-10-30).

1h. **Install and bootstrap.** A Rust conversion, true plugins, and Claude cloud agents that test fresh installs and adding nodes. This absorbs the parked gateway, plugin and bootstrap input (Tony, 2026-10-30).

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

(none)
