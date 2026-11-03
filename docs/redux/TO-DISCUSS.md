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
   - Queued under 1g: access levels and how they work. Tony: "We should discuss access levels and how they work at some point" (2026-11-02). This covers the admin and user ssh levels.
   - Queued under 1g from that answer: the separate system for singleton services; shogun and the map of the Arbutus nodes; less memory for the singleton automations on alcor.
   - Tony on the network for each edge (2026-11-02): "We need to go over the networking between valid edges, choosing internet, lan, tailscale, or zero tier. I'm at the device list for zeortier so would like to reduce. Arbutus machine access have to proxyjump through bastion from outside arbutus. Lan may reassign ip. I think zerotier gave access to wsl directly not sure, needs checking. You can turn on capella to investigate and shut it down after. Zerotier and tailscale are tools, not some standard i want to use. Lyra should be the same as capella, but its a laptop and i could be connecting from anywhere. Alcor, mizar, mira, and capella are all physically plugged into the same gigabit switch so hopefully we can reduce hops there."
   - Tony on WSL mirrored mode (2026-11-02): "Wsl was set up as non mirrored specifically. What are the issues with mirrored, if any? It just means windows and all wsl instances share the same port space? Docker also runs through wsl on capella." and "Sockeye requires ubc vpn implemented through a docker container, does wsl mirrored mess with that? No more tests on capella until morning."
   - Tony on testing the edges (2026-11-02): "I'm up now so let's test the proposed edges before committing. I will start lyra, please start capella for me."
   - Tony on the edge test (2026-11-02), verbatim:
     - "capella no coming up on its own needs fixing... I saw the machine turn on though... lyra is also on and online, but I havent set anything up yet"
     - "\"I don’t ssh to deneb without asking\" can we review this instruction? -> this leads to the other subject of having admin accounts for ssh and config/setup + normal operating accounts (the current accounts with sudo)"
     - "I'm still not clear on how wsl will work. I want to avoid mirrored networking since this has had issues in the past an im not sure these issues were fixed. I want to be able to directly ssh to all hosts as the wsl instance (there is an update_ssh manually triggered script for this, but ideally this becomes automatic on change without contant polling). Inbound to these should be to wsl. note: lyra (may not even be named correctly as old name was xps) doesnt start its wsl automatically. I typically just connected a tether to windows side for agentic access, which was mainly ms office + zotero etc automation"
   - Tony (2026-11-02): "if we set up tailscale windows side, do we get the wsl side as well and so fix the windows issue?"
   - Tony (2026-11-02): "we should use something like ansible to set this all up and make sure the fleet is coherent. what can we reach with ansible?" arbutus-infra already holds an Ansible tree (`tony` worktree, `ansible/site.yml`).
   - Tony (2026-11-02): "lets try setting up ansible for the swarm (note that there will be overlap with shogun, which manages non swarm machines as well. shogun also manages altair and shaula through ansible, but we should only ansible with shogun and altair)" and "maybe we just test and POC, but implement all at once so we can continue with the original task queue awm redux interview"
   - Tony on access levels (2026-11-02): "can you see where sudo is needed to eval if it is safe to drop from normal accounts? Docker? even on mizar which serves the general purpose and fleet admin role? How do I grant sudo without having to be at a terminal?" and "For the implemenation pass, it would be great if all of the work that requires me can be grouped together, like adding tailscale"
   - Tony on approvals (2026-11-02): "there are several approval mechanisms that are required now... I think it would be best if deneb hosted a page for this with a queue of pending approval events with a history of expired, approved, or cancelled. This can also be set up as a pass through page. The unit of pass through needs to consider netowrk efficiency but also would be great if we can have a single passthrough unit/modal for everywhere that needs it, served by alcor/mizar (what did we agree on here? I think alcor would be the better fit)."
   - Tony (2026-11-02): "we can really lock down alcor then -> can we sandbox the sensitive services from using alcor as supplemental compute? can we also force ssh through mizar, like how arbutus has bastion?"
   - Tony (2026-11-02): "what are the effects of rootless docker? does this make anything harder? Can rootless users still install stuff if needed (what is the line for this)?", "The ssh access is not the same as the singleton service access right? machines that need 2fa dont have to have an edge into alcor. So the services should still work with mizar down, we just cant ssh into alcor with mizar down." and "is there a way to set up more of a dynamic wall between the services and the rest of alcor for compute? I dont want to have to work inside containers on alcor. What is bubblewrap?"
   - Tony (2026-11-02): "test ubc vpn with rootless docker. tony and other normal usage users losing sudo seems to entirely depend on if we can use docker afterwards (like building metasmith or as a build image for rust, or vpn). What does rootless docker actually block? the ports below ~1k are not important to me, I dont use docker for live services -> though having the key singleton services as images will help with robustness and healing? this may be worth considering"
   - Tony (2026-11-02): "We should also do this for deneb? when we discuss the plugin rework, containers should become an option.", "cross architecture emulation is needed for rust builds. but a once only set up is fine." and "the vpn can use the current mira 2fa service try a real connect to sockeye. do a real test"
   - Tony (2026-11-02): "journal is a servce with different access than the rest of awm for agents, so it gets treated like one. Im not sure, but I think this expands rather than contradicts your systemd-journal group point."
   - Tony on the attachable browser (2026-11-02): "1: yes agree, but can we do windows? agents attach to their own window so they can manage tabs?" and "I think opera was choosen because it is lighter than chrome. this is prolly journaled on capella"
   - Tony (2026-11-02): "window per agent shape looks right -> would be great if we can have 1 attachable viewer process rather than 1 per window seat. I also think we may have to do containers per seat as well for isolation" and "can we run a test to see if memory is a concern between opera and chrome?"
   - Tony (2026-11-02): "sorry, seat is not a socials seat. seat is just each attached session to a browser window"
   - Tony (2026-11-02): "we also need the case where an agent on altair asks to read journals on capella, which should be grantable" and, on routing doors through deneb's relay, "for the relay, can that be used to establish p2p rather than full coms through deneb?"
   - Queued under shogun: Ryan's access. Tony: "Ryan also needs accesss to shogun at some point" (2026-11-02). arbutus-infra has a `ryan` worktree on altair.
   - Queued for the singleton subject: access to awm's own `journal` as a service with its own rules, beside the knowledge port (R90). Tony: "journal is a servce with different access than the rest of awm for agents, so it gets treated like one." and "we align, proceed" (2026-11-02).
   - Queued for the singleton subject: Tony: "Can we move the current opera stuff to the same mechanism? How will captcha and my signins get through? Discuss when we reach this subject" (2026-11-02).
   - Then: how `reflection` behaves across peers, once the network architecture settles (Tony, 2026-10-31).
   - Settled (`fleet.md` R105): deneb is the vault's sync hub, and a sync link carries no authentication. Trilium now runs only on deneb, walled, with a nightly copy on mizar behind a tripwire.
   - Settled by `fleet.md` R106 and `channels.md` R77: whether deneb keeps the reporting service. The task queue moves to alcor, and the front door goes.
   - Queued from 1f: pinned keys for peers that Tony verifies, which remove the relayed room password (`channels.md` R73, also thread 1j).
   - Settled by `fleet.md` R106: which node writes a task's card when its sessions span nodes. One task queue service on alcor writes every card.

1s. **Isolated sessions with awm tools.** Tony: "I've started an expedition session out of curiosity. Stuff like that should prolly get some form of isolation as well, but have access to awm tools in its environment -> something to talk about" (2026-11-02).

1h. **Install and bootstrap.** A Rust conversion, true plugins, and Claude cloud agents that test fresh installs and adding nodes. This absorbs the parked gateway, plugin and bootstrap input (Tony, 2026-10-30).
   - Local throwaway environments that behave like cloud agents, to avoid their cost. Tony: "we should also consider settup up a system that can spawn throway temp environments that act the same" (2026-10-30).
   - Plugins as their own projects, each with its own refresh hook, migrated out of the awm repo (Tony, 2026-10-31, R33).
   - Containers become an option for a plugin. Tony: "when we discuss the plugin rework, containers should become an option." (2026-11-02).

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
     - Socials seats cannot look up Tony's personal information. They use the browser without his credentials (`fleet.md` R102). Tony: "socials seats should be instructed to not attach to browsers. socials seats should be restricted from looking up my personal info..." and then "the browser utility is still needed. so lab seats get the service but not my credentials." (2026-11-02).

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
