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

**3a. The Slack seat system.** See the stack entry below.

Parent thread 1 (end state) waits for Tony to confirm `goal.md`. Its children 1a to 1t are closed.

## Stack

Top of the stack comes first.

3a. **The Slack seat system.** Review how seats work and discuss whether they keep their shape. Tony: "this is another system to review and discuss" (2026-10-30). The survey of today's seats is in `users.md`. Open for Tony:
   - Permanent home of social and seat agents, given the planned mira migration.
   - Who the 2 static edge users are.

4. **What made it foundational.** Which problem made Tony think "rebuild" rather than "patch"?

5. **Done.** What makes the redux finished?

6. **Review the protocol docs.** Add the deploy to the core awm docs, then review every protocol doc. It runs after the other threads settle. Tony: "we should also add the deploy to the core awm docs -> I guess we need to review all protocol docs once we settle everything this session." (2026-11-01).

## Pending sweep

Peer writes held until the end of the interview.

- Precedence `f7def9a0d79ac72f`: set the decision to "A task name is a slug of 2 or 3 words (e.g. redux-naming). The coordinator takes the task name. Each session it starts is named <task>/<descriptor>, with a descriptor of 1 to 3 words for its concern (e.g. redux-naming/survey). Keep names under about 40 characters as a guideline. Names serve legibility only. awm tracks sessions by the R9 session id (2-character platform code plus the Claude session id) and tasks by a task id that awm mints for each card: the first 8 characters of a random UUID in URL-safe base64. Tony: "Id  prefer human readable prefix and session descriptor but fear will get long", "lets omit the id in the name. Names can be for legibility only." and "The real tracking is the 2 character claude indicator plus claude session id for sessions and the task ids minted by awm" (2026-11-01)."

- New precedence for metasmith: "Install tests of metasmith run first in a Claude cloud agent, with a local machine as the fallback. Tony: \"cloud first and same for metasmith.\" (2026-11-02)." It mirrors `install.md` R118 item 2.

## Parked inputs

Raised before the redux started. Each enters the stack when a thread reaches it.

- Unified permissions across boxes, Tailscale included, and the migration to it (Tony, 2026-10-29).
- A Slack-only box on Arbutus, and the compute model (Tony, 2026-10-29).
- Migrate mira onto mizar or alcor (Tony, 2026-10-30). mira hosts `social` and the seat config today.
- One always-on shared Chrome on alcor or mizar for agents, and for Tony to solve captchas (Tony, 2026-10-30).
- Three workstream sessions on altair (`c4455c36`, `bc32a23b`, `e75d93a3`) disappeared after 2026-10-30 03:07Z. Resume them or leave them stopped.
- The Claude Code daemon does not restart pinned jobs after a host reboot.
- Armando's machines, after Tony meets him (`fleet.md` R120). Open items: the Windows version, admin rights and UBC IT policy for Tailscale and the node, instrument PCs kept offline, which plugins a Windows station runs (a third-party plugin needs a Linux container, `install.md` R116), a smoke test of a blind build on a Windows runner, the tailnet tag and its access rules, and an exception for these machines to the rule against ssh to UBC hosts. The interview proposed the last four. Tony has not decided them.

## Closed

- 3. Who uses it (2026-11-02). Result: `users.md` R121 to R124. awm's users are its four interface boundaries: seats for humans, the queue for the owner, rooms for agents and swarms, and bash, ssh and the tether for machines. A foreign swarm comes in through rooms. Ryan approves Arbutus changes through Slack, a set apart from Tony's. The public never calls in directly. Tony: "public shouldnt be able to call into awm directly. that is a major overwhelm risk"
- 2. Why awm exists (2026-11-02). Result: `goal.md` § Why awm exists. awm is the infrastructure for how Tony works with agents across machines. It queues work to him in one line, holds his ideas for later, turns intent into parallel steps, keeps private and lab work apart, and keeps what each run learns. Tony: "Kindof like hyperthreading a CPU by some perspectives."
- 1j. IT services through the tether, and proof of ownership (2026-11-02). Result: `channels.md` R75. Tony dropped the thread. Consent is the owner's yes to the operator, whoever that is. The QR challenge goes, because tethers mostly run one way, outbound from the agent. Tony: "Lets just drop this and move on."
- 1i. The Armando box (2026-11-02). Result: `fleet.md` R120. One or more machines of Armando's metabolomics facility at UBC join for a project as temporary Windows stations, native and not in WSL, on the tailnet, with a run-and-done tether that Tony opens at the keyboard. Install and removal repeat through cycles of investigation and blind builds. The details wait for Tony's meeting with Armando.
- 1h. Install and bootstrap (2026-11-02). Result: `install.md` R112 to R119. A Rust core built in a container on mizar, plugins as projects with a manifest and guards, dependencies through tool UUIDs, an install test that runs on request in a Claude cloud agent first, and public images on quay.io.
- 1t. Rooms and the tether as one awm tool (2026-11-02). Result: `channels.md` R110 and R111, `fleet.md` R109 item 2. One `channel` tool opens a room or a machine. They share the relay, the codes, the capped push, the verbs `open`, `close`, `write` and `status`, and the close on task end. A room adds `encrypt`. Isolated sessions may hold both for their own task.
- 1s. Isolated sessions with awm tools (2026-11-02). Result: `fleet.md` R103 item 1 and R109. A starter may set a session's trust equal or lower. An untrusted session runs in a rootless container with the host's Claude install, a whitelist of awm tools, no ssh and no messages out. Its login holds only an access token that the host refreshes, so a fault signs out the container, never the host.
- 1g. Communication between awm nodes (2026-11-02). Result: `fleet.md` R86 to R108, `channels.md` R73 and R77, `queue.md` R64. Foreign and domestic peers give way to a directional edge list that matches ssh, one network per edge, singletons on alcor behind their own door, admin grants in place of sudo, browser seats, trust per session, Trilium only on deneb, one task queue on alcor, and journals as a service.
- 1r. Harden deneb (2026-11-01). Result: `fleet.md` R79 to R84, `queue.md` R64 and R85. deneb keeps the vault, Zotero on a Sync now button and the relays. It runs no agents, no Penpot and no game server, and opens no outbound ssh. The vault and every gateway log in with the passkey, over Tailscale too. Recovery runs over ssh to mizar from capella or lyra.
- 1f. The three channels (2026-11-01). Result: `channels.md` R69 to R77, `sessions.md` R78. All channels share one shape and carry a trust level set per seat. Agent rooms run on deneb from a three-word invite, plaintext by default with SPAKE2 on request, and have no inbound door. A live tether can raise its owner to trusted through the passkey. Thread 1j dropped that raise.
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
