# Fleet board design (draft)

## Purpose & Contents

This file is the design draft for the rework of the Swarm board: how Tony sees and talks to sessions on every box from one place, and how seats fit. It builds on the circles and the crossing contract in `cloud/tencent` `FLEET.md` (workstream 1). It holds:

- the decisions only Tony can make, each with a recommendation,
- how the board works today, in brief,
- the issues, numbered I1 to I10,
- the goals, numbered G1 to G6,
- the options for each design area, each with a recommendation,
- the migration order,
- the gaps this survey found in `FLEET.md`, for workstream 1.

It changes no code. The operator contract stays in `awm/services/reporting/INSTALL.md` and `FEDERATION.md` until a design is chosen. This draft replaces neither.

## Decisions for Tony

Each line is one decision, with the recommendation first. Section references point to the options below.

1. **Where the Swarm board lives.** Recommend: take the Swarm board out of the shared knowledge vault, whose hub is deneb, the public circle. Host it on mizar, in the personal circle. mizar pulls each node's ledger and is the only writer of the board (§ A, option A3). The alternatives are a vault hub on mizar (A2), or no change (A1).
2. **What the board looks like.** Recommend: a board-only Trilium instance on mizar, so drag, chips and the Trilium fork stay as they are (§ B, option B1). The cost: a claim can no longer place one of your vault notes on the board as the card itself. The card links to the note instead.
3. **Where you reach the board from.** Recommend: the tailnet only, through mizar's edge. Use claude.ai Remote Control from the phone. The alternative keeps a route through deneb's public edge, which puts the board back in the public circle.
4. **Remote Control on every background session.** Recommend: yes. Every card then links to the session's claude.ai page, on any box (§ C, option C1).
5. **Your message on a card goes into the session.** Recommend: yes, on the board-only instance, where every chat note is yours (§ C, option C2). This is also how you answer a stuck modal from the board (§ D).
6. **Seats on the board.** Recommend: a standing Seats column, with one card per kept seat that shows its health, node, agent and link. A seat card goes to Blocked as it does today (§ E, option E2).

Recommendations 1, 3 and 5 depend on decision 3 in `FLEET.md` (mizar in the personal circle) and on mizar being always on. Confirm both.

## How it works today

- Each fleet node runs `reporting`. It keeps a SQLite ledger of its sessions' tasks and writes each task as a card note in the Swarm board, a Trilium book in the shared vault.
- The vault is one document in a star. deneb is the hub. altair and capella are spokes over an ssh port forward. An edit anywhere reaches the others within about a minute, and the last write per note wins. mira, mizar and alcor run no Trilium.
- A node writes cards through its own loopback Trilium only, because the note verbs refuse every edge caller.
- A card opens on a plan, `request_human`, `task_track`, a cx worker's first prompt, or a background job that starts to wait on Tony (37bc9dfb). The sweep moves a card on a wait, a stop, a retire and Tony's drag.
- The Agent line links a card to the session's claude.ai page while Remote Control is on, and shows `claude attach <job>` otherwise.
- `reflection` drives one node's sessions. With `peer`, it drives another node's. `message` and `interrupt` refuse while a modal shows.
- The federation board runs on deneb for foreign swarms. The front door on altair takes in its cards.

## Issues

- **I1.** A node without a Trilium replica shows no cards. mizar's `seat:muse` waits on Tony now, and the board does not show it.
- **I2.** A card's node is whatever the writing process says it is. A stray gateway on deneb wrote cards and an Ops note under the hostname `VM-0-8-ubuntu`, and nothing checked the claim.
- **I3.** The Swarm board sits in a vault whose hub is deneb, the public circle. deneb can read and rewrite every card, plan and attach command, and its edits replicate into altair and capella. This breaches crossing contract rules 2, 3 and 6 in `FLEET.md`.
- **I4.** A card carries no conversation to its session. A message on a card wakes nobody, and only Tony's next prompt in the session unblocks it.
- **I5.** A card links to its session only while Remote Control is on. `start_other` leaves it off by default.
- **I6.** A session stuck at a Claude Code modal clears only at its terminal, because `reflection` refuses to type into a modal.
- **I7.** Fleet-wide reach runs from altair today: the triage session lists mizar's sessions, and `start_other` takes any `peer`. Under `FLEET.md` the university circle never reaches the personal circle, and capella already refuses altair's ssh key. A fleet view hubbed on altair breaks under the contract.
- **I8.** Seats show only when they wait on Tony. No place shows which seats are alive, who holds each one, or the per-person agents behind them.
- **I9.** An interactive session shows nowhere until it plans or calls `request_human`. That is by design (precedence `b09dc2566a0d059a`), but it also leaves its link and node unknown.
- **I10.** Two boards share the word "card" and two verb families (`board_*`, `swarm_*`). Agents used the wrong one twice (journals `b0aecc57`, `fe04070b`).

## Goals

- **G1.** Tony sees every session that waits on him, on any box, in one place.
- **G2.** Tony talks to any session on any box from that place, a stuck modal included.
- **G3.** The board obeys the circles. A lower circle cannot read, forge or steer what a higher circle shows or does.
- **G4.** A card's node is a fact the board observes, never a claim its writer makes.
- **G5.** Seats show as standing seats with their health, not as tasks.
- **G6.** What Tony does today keeps working: a drag moves a card, `claim task <ref>` claims, `request_human` blocks, `/new-agent` starts an agent.

## A. Where the board lives

**A1. No change.** Every node with a replica writes into the shared vault. This fails G1 for mizar and alcor, G3 because of deneb, and G4.

**A2. Move the vault hub to mizar.** mizar becomes the hub and every fleet node becomes a spoke, mizar included. This fixes I1 for nodes that take a replica. It keeps the shared-writer model, so I2 stays. Every univ node still writes upward into a personal machine's document. It also moves the public slice and nexus `/trilium` off deneb, which is workstream 3's ground.

**A3. Pull from the top. Recommended.** The ledger on each node stays the source of truth, as it is today.

1. Each fleet node serves one read verb, `operations ledger_read`. It returns the node's open tasks, its current waits, and its session roster from `cx list`. The verb is domestic-only and has effect `read`.
2. mizar runs the board host role of `reporting`. It calls `ledger_read` on every node with its node token on a 30-second tick. It renders the cards into a board-only Trilium instance on mizar. Nothing else writes there.
3. Tony's actions on the board go downward as commands to the owning node: a drag calls `task_move`, a chat note calls `reflection message`, a modal answer calls the modal verb (§ D).
4. A node's identity on a card is the peer the host called. No writer can claim another node.

A3 meets G1 to G4. A node needs no Trilium to show its cards. A lower circle has no write path into the board at all, because a ledger row is the card that crossing contract rule 3 asks for. When mizar is down, the board is down and every ledger keeps running. Nothing is lost.

**CAUTION** Under A3 a claimed federation card can no longer be the same note on both boards, because the stores split. The board host shows a card that links to the federation card instead.

## B. What the board looks like

**B1. A board-only Trilium on mizar. Recommended.** It keeps the board view, the columns, the chips, the fork and the drag. It runs with no sync, so the host is the only writer and the last-write-wins conflicts end. A claim by a vault note URL makes a card that links to that note.

**B2. An awm page on mizar's edge.** A purpose-built fleet console: a session roster, cards, a send box and modal buttons. It fits talk and modals better than Trilium does. It is new UI that Tony has not used.

**B3. Both.** Start with B1. Build B2 only when the card chat and Remote Control prove short for talk.

## C. Talking to a session

**C1. Remote Control on every background session. Recommended.** `start_other` turns it on by default, as `/new-agent` does now. Every card's Agent line becomes a claude.ai link, on any box. Tony talks to the session from claude.ai or the phone app. The conversation crosses no circle on our mesh.

**C2. The card chat goes into the session. Recommended, with C1.** A chat note Tony adds under a card goes to the host. The host calls `reflection message` on the owning node, downward. The session answers with `swarm_message_post`, which goes into its node's ledger, and the host pulls it onto the card. On the board-only instance every chat note is Tony's, so authorship holds without a signature. C2 is the asynchronous path: it works while Remote Control is off or disconnected, and it keeps the exchange on the card.

**C3. Terminal only.** Today's `claude attach` and `tmux attach`. Keep it as the fallback.

**CAUTION** Never accept C2 on a board that a lower circle can write. On today's vault, deneb can add a chat note that reads as Tony's, and the host would type it into a univ session.

## D. Clearing a stuck modal

**D1. Remote Control answers it.** Check first which modals claude.ai shows: permission prompts and `AskUserQuestion` likely, TUI-only dialogs perhaps not.

**D2. A guarded modal verb. Recommended for what D1 cannot reach.** A new `reflection` verb reads the modal on screen and returns its text and numbered options. With `choose`, it presses one option. It refuses a modal whose shape it does not know. cx's `needs` line already carries the modal into the card's waiting-on reason. Tony replies on the card with the option number, and C2 delivers the answer through D2.

**D3. Terminal only.** Today's state.

## E. Seats

**E1. Today.** A seat gets a card only when it waits on Tony.

**E2. A Seats column. Recommended.** One standing card per kept seat, from `social keepers` on mira: the seat, its node, its health, its agent job and its link. A seat card never moves through the task columns. A seat that waits on Tony shows in Blocked as it does now. The per-person agents a seat starts show as a list on the seat's card, and get a task card only through `request_human` or a wait. This keeps routine lab work off Tony's board, as precedence `89e3306ab5f9656f` asks.

A seat box reads untrusted text, so the host renders its rows as plain text and sends it commands only downward. That holds for deneb now and for the planned Arbutus seat box.

## F. Node identity now

A3 makes the node a fact. Until it ships, have `reporting` refuse to start when `node_name()` falls back to the hostname, or names a node that its own peer book does not hold. This closes I2 cheaply. The stray-gateway fix 12e4fc8f removed one cause, not the class.

## Migration order

1. Turn Remote Control on by default for `start_other`. Make `reporting` refuse a hostname identity. Both are small and help today.
2. Add `ledger_read` on every fleet node.
3. Stand up the board-only Trilium and the board host role on mizar. Run it beside today's board for a few days. Compare the two.
4. Add the downward commands: move, chat delivery and the modal verb.
5. Cut over. Nodes stop writing to the vault board. Archive the old board in the vault.
6. Add the Seats column.

Steps 3 onward need mizar to reach every fleet node's edge over the tailnet, and a node token pinned for mizar on each. They also need the `circle` field from `FLEET.md` migration step 6, so that `ledger_read` and the commands accept the personal circle only.

## Gaps for workstream 1

The survey found three cross-circle paths that `FLEET.md` § Live gap does not list.

- **The knowledge vault hub is on deneb.** The whole vault, private notes included, replicates between deneb and the univ and personal spokes. A note planted on deneb runs as a frontend script in Tony's browser on altair's or capella's origin, with his session (trilium `INSTALL.md` accepts this risk for a shared origin). Backend scripting is off everywhere.
- **The Swarm board is in that vault.** This is I3.
- **altair reaches personal nodes for sessions.** `reflection list_other` and `start_other` with `peer=mizar` work from altair today. This is I7.

## Still unsure about

- Is mizar always on? The design assumes so. capella sleeps, so it cannot host the board.
- mizar's peer book holds altair, deneb, mira and alcor with pinned keys, checked 2026-10-30. It holds no capella entry. The host needs one.
- Which Claude Code modals does Remote Control show on claude.ai? D2's scope depends on it.
