# Trilium design — decisions

## Purpose & Contents

This file holds each closed topic of the trilium-design discussion with Tony, in the order the topics closed. Each section gives the question, Tony's deciding words, the design and where it landed. A section that a later topic changed names that topic. The open state is in `STACK.md` beside this file. The spec items in `docs/redux/` are the source of truth for the build.

## status-pages (2026-11-07)

Question: what the swarm status page (Ops) is, who gathers its facts, and where it lives.

Tony: "ops can be the new swarm status page I requested and all of our nodes including stations register to it.", "maybe we need a telemetry core plugin that gathers data", "can we build this in rust?", "can we go with active triggers rather than passive 1 min scans?", "ops is a page. it calls and responds to multiple services", "usage can honestly just be number of sessions in the last 7 days."

Design:

- A new core plugin `telemetry`, in Rust, holds facts only. Its part on each node forwards events on the gateway's emit streams: a session starts, stops or changes Remote Control, and a service comes up or goes down. Its part on alcor keeps the roster. It does no polling.
- `operations` keeps the queue, the approver and the relay. reflection on the node carries out an action.
- Ops is a note in the vault, as the board is. The `trilium` plugin renders the roster into it on each telemetry event. Changed by page-origin, which replaced the frame from alcor of 2026-11-06.
- One large card per node, always expanded: name, role, portal link, last seen, and its sessions with their claude.ai links. A fuzzy search filters by node and session name.
- A node's usage is its count of sessions in the last 7 days.
- A services section lists each singleton with its host, health, last response and last use. Each node card lists the services it can use, with last check and last use.
- A node opts in at `awm install`.

Landed: `fleet.md` R147 items 1-6, `install.md` R115 item 4 (telemetry in the core list).

## new-agent-button (2026-11-07)

Question: what replaces Proposed's new-note button, and how Tony starts an agent on a chosen node.

Tony: "new note for proposed simplifies to new agent -> which honestly should actually be a button on the board page, which lets me start an agent with remote control on a specific node. nodes can be sorted by usage.", "also with no brief so it doesnt start going off without direction", "makes sense, name scope lobby", "drop" (Proposed).

Design:

- One New agent button in the board page's header. Its form is a node dropdown only. It lists the nodes that run agents, fewest sessions in 7 days first. Stations and deneb are not listed. The dropdown reads the roster in the vault (changed by page-origin).
- A press starts an agent with Remote Control, no brief and no prompt, in the scope `awm/lobby`. The agent waits for Tony and moves itself to the right scope once it has work.
- The task's card opens in Needs you with the session's claude.ai link. Tony's first prompt moves it to Working.
- Proposed is gone. The card-level Spawn agent button and the new-note action are gone.

Landed: `queue.md` R61, R63 items 1-2, R66; `fleet.md` R106 item 4.

## seat-rc-toggle (2026-11-07)

Question: how Tony turns Remote Control on and off for seats, so seats stay out of the claude.ai sidebar.

Tony: "accept. inferred: seats with RC toggled on stay that way even across empty/restarts. this also means that empty seats can be toggled".

Design:

- Ops has a seats section: every seat from social's roster, its state (empty, occupied or retiring) and one Remote Control switch.
- The switch is a setting of the seat, held by social. It holds across empty spells and restarts. An empty seat can be switched.
- A seat starts with Remote Control off. Switching an occupied seat also switches its live session. A seat that is on shows its claude.ai link.
- A press is an everyday action (changed by page-origin). An order for a session in mid-turn waits for the turn to end.
- claude.ai has no native sort, filter or hide for its session list, only Archive.

Landed: `fleet.md` R147 item 7, `sessions.md` R146.

## page-origin (2026-11-07, first pass)

Question: where the board, Ops and their buttons are served from, and how a press is authenticated while deneb stays untrusted.

Tony: "I want ops and the control panel to be pages in trillium, essentially notes", "if we made signin/new devices, etc, gaining identity an auth page on alcor, then used that to sign API actions from deneb, can we then stop all of the threading stuff on deneb and just have it serve the trillium pages that call to alcor as a server backend?", "lets try this as a first pass. I will make more passes at trying to find a good balence for convienience and security."

Design:

- deneb serves every page as a vault note with its own code: the board, Ops and the per-note button.
- alcor is the backend behind an API at `auth.tony-xy-liu.com`. It also has one page of its own there, for identity.
- The sign-in leaves a device credential, a cookie on alcor's address that page code cannot read. The vault's pages call alcor's API with it. alcor checks the credential and the request's origin.
- An everyday action carries no instruction and no grant: a read, New agent, a task from a note, a seat switch, closing or rejecting a card. An agent takes instructions only from Tony over Remote Control. The worst a hostile deneb does is a nuisance Tony sees and undoes.
- The R65 hash chain and the panel frames are gone.
- Approvals are open: see board-look.

Landed: `queue.md` R64 item 5, R65, R137; `fleet.md` R97 item 2, R106 item 2, R147 items 2, 3 and 7.

## signin-harmonize (2026-11-07)

Question: how the vault, alcor and each node's landing page share one sign-in.

Tony: "We can have a harmonized sign-in page on alcor via tailscale, that then links to all of the others? for 12hrs? Then I can visit any node or nexus and be signed in automatically?", "part 1 direct visits should redirect to the sign in page and redirect back on sucess", "part 2 nexus only is fine. I suspect subdomains for every node is not manual churn I wnt to keep up with".

Design:

- One sign-in page on alcor's public address, not its tailnet name, so a phone with no VPN reaches it. One passkey tap starts a 12-hour session, which also sets the device credential.
- The page links to every surface. Each link carries a short grant that signs Tony in on arrival.
- A direct visit with no session redirects to the sign-in page and back on success. With a live session the page succeeds without a tap.
- On the vault the sign-in is a frame from alcor, because the vault and alcor share the site `tony-xy-liu.com`. A node's landing page keeps the redirect. Nodes keep their tailnet names.

Landed: `fleet.md` R84 items 1-2, `queue.md` R64 item 5.

## note-agent-button (2026-11-07)

Question: what the per-note bot button does, and how it ties a note to its task.

Tony: "it basically shortens what I have to type to start a task with card, but it is still using/funneling into the task system", "just have the marker card link to the real task card rather than full copy with stats", "I think the new task from this button is also not a good idea. a simple new agent is fine, then I can reference other tasks by name or id".

Design:

- A bot button sits beside Trilium's create-child button on every ordinary note. It opens the New agent node dropdown.
- A press starts a task with a card. The card carries the note's reference. The agent reads the note as context only and waits for Tony.
- The card holds a clone of the source note. Under the source note, the writer adds a marker note that links to the card. Retiring the agent deletes the marker. Both directions cannot be clones, because Trilium refuses a cycle.
- A card's note has no such button. Tony starts a New agent and names the other task by its id.
- The writer's reach outside its roots widens by two acts only: its own labelled marker notes under any note, and a clone of a source note under a card.

Landed: `queue.md` R63 item 3; `fleet.md` R106 item 2.
