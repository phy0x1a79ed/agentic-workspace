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

**1g. Communication between awm nodes.** See the stack entry below.

Parent thread 1 (end state) waits for Tony to confirm `goal.md`. Its children are 1a to 1q.

## Stack

Top of the stack comes first.

1g. **Communication between awm nodes.** Tony: "This doesnt cover coms between nodes of awm itself which needs discussion. I think we need to drop foreign nodes in favour of a searchable endpoint instead" (2026-10-30).
   - Tony stepped back (2026-11-02): "This was to consolidate mechanisms and access patterns since a lot of the mechanisms werent being used or respected. We should go over the access patterns for each peer and look at existing cross peer access patterns and design a system that actually fits how networked awm peers is used."
   - Tony: "In addition to the decided seats, rooms and tethers, i want a deterministic knowledge port as well. We should hand out timed access keys and use them to log when, how and what acessed." (2026-11-02).
   - Tony: "I want to drop the foreign and domestic system because it wasnt used. Need survey of current cross peer hsage examples before designing replacement protocol." (2026-11-02).
   - Tony: "I think basically, agents need to be able to interact with other agents across peers as if they were on the same machine. This should be directional. Peer lists are really edge lists and is expected to be assymmetric. Need to also revise the concept of stations. For robustness of the swarm, making agents capable of running on stations but making it not the norm would be beneficial." (2026-11-02).
   - Tony on the edge list (2026-11-02), verbatim:
     - "Edges should match ssh permissions"
     - "Lyra/xps is my laptop, capella is my desktop. These should connect out to all other nodes and be reachable from almost none except mizar."
     - "Mizar is home base, it reaches all and only alcor and my 2 machines can reach it."
     - "Deneb, sockeye, and fir are stations. They reach nothing but should be reachable by all. Agents arent supposed to run on them. They provide supplemental compute and deneb provides public facing endpoint/relay."
     - "Alcor is supplemental compute for mizar. I want to have it take over mira soon. I think i can transfer Mira's drive to either alcor or mizar. One of alcor's jobs will be to host long running crucial singleton processes like my 2fa auth, a real chrome browser to attach to thay i can also login and do captcha challenges, other sensitive auromations like ms teams, downloading attachments from google drive gmail, etc (can we work to reduce these btw? The current aporoach isnt memory efficient). So nothing should be able to reach alcor except mizar +my 2 machines, but all peers need to be able to use alcor's singleton services. Alcor only can see only mizar"
     - "Altair is my work node. It can see sockeye and fir. It shouldnt know about deneb, but it should still be able to access its singleton services (the task board). Altair sees nothing else. Mizar and my 2 machines see altair."
     - "Can you draw a square table for this? This is ssh + agentic access. We need a separate system for singleton services. We also need a separate admin and user ssh access levels. So shaula is manageable through an admin account fir example. Altair and shaula are part of arbutus cloud. I want a small machine to manage arbutus so that the lab work stuff is not mixed. This also goes for worker 1 and worker 2 and showtime, and bastion. I guess there are a lot of arbutus peers and stations that need to be mapped and brought under the management of the new admin node. Lets call it shogun."
   - Queued under 1g from that answer: the separate system for singleton services; admin and user ssh levels; shogun and the map of the Arbutus nodes; less memory for the singleton automations on alcor.
   - Then: how `reflection` behaves across peers, once the network architecture settles (Tony, 2026-10-31).
   - Queued from 1r: deneb is the vault's sync hub, and a sync link carries no authentication, so deneb can rewrite every note on every copy (`fleet.md` § Gap).
   - Queued from 1r: whether deneb keeps the reporting service, which mirrors the ledger onto the board.
   - Queued from 1f: pinned keys for peers that Tony verifies, which remove the relayed room password (`channels.md` R73, also thread 1j).
   - Queued from 1d: which node writes a task's card when its sessions span nodes, given that mizar holds no vault copy (`sessions.md` R58, `queue.md` R64).

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

- 1r. Harden deneb (2026-11-01). Result: `fleet.md` R79 to R84, `queue.md` R64 and R85. deneb keeps the vault, Zotero on a Sync now button and the relays. It runs no agents, no Penpot and no game server, and opens no outbound ssh. The vault and every gateway log in with the passkey, over Tailscale too. Recovery runs over ssh to mizar from capella or lyra.
- 1f. The three channels (2026-11-01). Result: `channels.md` R69 to R77, `sessions.md` R78. All channels share one shape and carry a trust level set per seat. Agent rooms run on deneb from a three-word invite, plaintext by default with SPAKE2 on request, and have no inbound door. A live tether can raise its owner to trusted through the passkey.
- 1e. Seats as sessions (2026-11-01). Result: `sessions.md` R67 and R68. A seat stays one long-lived session on its channel and turns each real request into a task. It acts on the queue only through Tony's passkey.
- 1d. The pending queue and the active list (2026-11-01). Result: `queue.md` R60 to R66. A four-section command center page in Trilium replaces the Kanban. mizar authorizes every action through an embedded panel, and deneb is an untrusted station (`fleet.md`).
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
