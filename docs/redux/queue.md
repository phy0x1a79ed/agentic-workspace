# Queue

## Purpose & Contents

This doc holds Tony's interface to the swarm: the Swarm board, its stages, what its cards show and how they update, what happens to old cards, and how one click on a card starts a conversation with its agent. Thread 1d produces it. `goal.md` § Core loop steps 4 and 5 set its frame.

How a session closes and raises follow-up issues belongs in `sessions.md`. What holds a scope's claim belongs in `scopes.md`.

## Requirements

- **R60.** The board improves its cards, simplifies its stages and handles old session cards better. The change removes more than it adds. Tony: "add to the cards, simplify the stages, and add a better mechanism for old session cards." and "I think a lot of this can be done by just improving the cards in terms of what they show and how they are updated and actually removing things rather than adding" (2026-11-01).
- **R61.** The Swarm board is a new page built in Trilium, a command center. It replaces the Kanban. It holds four collapsible sections of card headers. Tony: "we are updating the kanban board to 4 collapsable sections of card headers (title and tags/chips, no content) with info panel on right for sleected card. The kanban is no more.", "i think actually having a new page (but still built in trillium please) is needed", "This is basically a command center" and "I really want this to be on trillium so that things are harmonized" (2026-11-01).
  1. **Proposed** holds Tony's notes for future tasks. Tony: "Need a fourth proposed section with button for new note cards that defaults to markdown for me to describe future tasks."
  2. **Needs you** holds every card that waits on Tony. A status tag on each card says why, such as planned or blocked. Tony: "3 collapseable sections. Needs you, working, done. With status tags like planned, blocked, etc."
     - Tony's prompt to a session moves its card from Needs you to Working. Tony: "agree" (2026-11-03).
     - An urgent request also sends Tony a Discord direct message. Tony: "i still want urgent requests to ping me via discord dm" (2026-11-03).
  3. **Working** holds the active cards, with no status tags. Tony: "Working prolly doesnt need status tags."
  4. **Done** loads older cards as Tony scrolls. Tony: "Done should do the infinite scroll element that loads more previous cards as needed."
  - Each card carries a dedicated chip for its peer. Tony: "Cards should have a dedicated tag or chip for peer."
  - The page has a search. Tony: "Search is needed."
- **R62.** A card shows only its header: its title and its tags and chips, with no content. A selected card shows its details in a right info panel. Tony: "Cards should be title only, but shows info when selected. This looks like we need a right info panel."
  - The panel's metadata is a table of every session of the task (R58), each with its status, active or inactive, and a link to each session that has Remote Control. Tony: "The metadata section (what we have now) needs to list all sessions, their status (active, inactive) and link out to the sessions that have remote control. This looks like a table."
- **R63.** Action buttons act on the selected card. Tony: "Prolly the better way is to make cards selectable and have action buttons, which covers new tasks from done and active."
  1. A new note button in Proposed creates a card with a Markdown body.
  2. A spawn agent button converts a Proposed card into a task and starts its agent on the peer that a dropdown names. Tony: "Need a button spawn agent from these cards which converts them. + dropdown for peer."
  3. A card in Working or Done starts a new task from that card.
  4. Closing and rejecting a card are buttons too.
- **R66.** Cards update one way. Only awm writes a card's section, tags and chips. Tony changes a card only through the action buttons (R63), and the card follows the task record. Tony writes the body of his own Proposed notes. Tony: "This makes sense." (2026-11-01).
  - Nothing reads a hand move back from the page. The Older books and the separate Ledger notes go. Done's infinite scroll (R61) and the sessions table (R62) replace them.
- **R64.** alcor authorizes every action, as a singleton service shaped like 2fa (`fleet.md` R97). deneb hosts the board in Trilium and is an untrusted channel. Tony: "Passkey + trusted device arbitraged by mizar with deneb viewed as an untrusted channel seems good." (2026-11-01). Tony moved the approver from mizar to alcor: "the approver is a singleton service that looks liek the same shape as 2fa, which is why I suggested alcor, what do you think?" and "we can really lock down alcor then" (2026-11-02).
  1. Each trusted device enrols a passkey once. A new device enrols only with approval from a trusted device, or through recovery (R85). alcor keeps each public key.
  - Every login and every request takes the passkey, over Tailscale too (`fleet.md` R84). An action inside an open window (below) takes the device key alone. Tony: "With r84, i need a passkey even over tailscale" (2026-11-01).
  - One passkey approval may cover a batch of actions, or open a window for actions of a stated scope. Every request still takes the passkey, but a request can be large, so Tony approves rarely. Tony: "Pass key on every action, but the discussion outcome was to let passkey approval request contain a batch of actions and/or open a window. So still approve on every request but the scope of requests can be large so that i dont have to keep approving" (2026-11-03).
    - A window lasts at most 12 hours, and a risk signal (item 2) closes it. Tony: "-> inflight action" and "otherwise accept" (2026-11-03).
  2. alcor weighs risk signals, such as a new device, a new network or an unusual rate. A risk signal asks for the passkey again. Tony: "Risk signals also seem like somwthing i want."
  3. The design works on a phone with no standing VPN link and with nothing to install. Tony: "This needs to work with phones which dont play well for constant link on a vpn like tailscale." and "Is there no built-in browser mechanism to prevent the install of the app (or executable or softare for linux, mac, windows, android, iphone... this becomes pain)?"
  4. The implementation uses existing packages for these standards. Tony: "Since this all seems like established standards, we should use existing packages when implementing".
  - deneb cannot read what passes through it. alcor opens an outbound tunnel to deneb, and deneb forwards the TLS of alcor's address down it unopened. Only alcor holds that address's private key. A CAA record limits certificates for the address to DNS validation, and only alcor holds the DNS credentials.
- **R65.** The card's action buttons are a light panel from alcor's own origin, embedded in the board page. It holds the device key and a hash chain with alcor. Inside an open window (R64) a press needs no action from Tony beyond the button. Outside one, the press asks for the passkey. Tony: "Whatever lets the device hold the chain locally, i dont want to have open on every prompt. It just keeps a ledger with mizar to prevent man in the middle attacks" and "Ok,  this looks good." (2026-11-01).
  1. The panel loads with the page, not at first use. Tony: "The mizar elements should also be light and load with page rather that at use to minimize performance impact."
  2. The device and alcor each keep only the latest hash. The device signs the full action message together with the latest hash. The new hash covers the previous hash, the message and the signature. alcor acts only on the exact message that the signature covers, so deneb cannot reuse a signature on another message, or a second time. Tony: "the signed message needs to be worked into the hash so that deneb cant use my signature and  replace the message" (2026-11-01), "I want a chain on device and mizar" and "the crypto ledger is essentially latest hash only. We dont need history, just need to know when it doesnt match up."
     - A spawn acts only on the Proposed body that Tony signed. Spawn refuses a note whose body changed after the press. Tony: "-> inflight action" and "otherwise accept" (2026-11-03).
  3. alcor acks each action with its new hash, signed with alcor's key. The device sends no new action until the last one is acked. Tony: "Some automated ack mechanism prevents advancement on internet drop (device can be ahead, but not behind)."
  4. After a drop, alcor's hash must equal the device's acked hash or its in-flight one. An in-flight action is a press the device signed and sent with no ack back yet. alcor refuses an in-flight action 60 seconds after its signing, and the device drops it. Tony: "-> inflight action" and "otherwise accept" (2026-11-03). Any other mismatch locks the device until the passkey.
  - Each device keeps its own chain with alcor.
  - The browser's same-origin rule keeps deneb's page code out of the panel's storage and buttons. deneb cannot sign an entry, so it cannot inject a command.
- **R137.** 2fa approvals reach the queue page, which holds 2fa buttons at all times, so Tony can use them whenever he needs to. The Discord channel for 2fa stays as well. A button press goes through the action panel (R65), as every other action does. The strip arms a burst of 1, 2 or 4 approvals, as Discord `/approve` does. Tony: "also 2fa approvals go through discord -> that should go to the issues queue page with contant buttons in case I need to use them. I think I want the discord channel as well" (2026-11-03).
- **R145.** Two tools serve the queue's concerns: `operations` and `trilium`. The core plugin `operations` holds the task queue (`fleet.md` R106) and the approver (`fleet.md` R97). Its store and approver run on alcor, and its relay runs on every node. `trilium` is the only tool that reads or writes the vault, the board's cards included (R105). The `reporting` and `board` services go, and no `queue`, `queue-store` or `approver` tool exists. The board goes only after the federation threads move onto persistent rooms (`channels.md` R72 item 5). Tony: "queue and approver go to operations. From what you listed, only 2 tools are sufficient for concerns, which are trillium and operations" (2026-11-03).
- **R85.** Tony recovers from the loss of every trusted device over ssh to alcor from capella or lyra. One command on alcor prints a one-time enrol link. The same path enrols the first device. Tony: "Losing everything, i should be able to ssh into mizar from capella and lyra" (2026-11-01).

## Decisions

- 2026-11-01: The inter-swarm board goes. Thread 1f decides how foreign agents communicate in its place. Tony: "Wr shoukd also remove the inter swarm board -> to be discussed with how foreign agents communicate".

## Gap from today

Surveyed read only on 2026-11-01 from the `feat/redux` source, and brought up to date on 2026-11-03.

- **No task queue is live.** The `operations` core plugin (`awm/plugins/operations`) holds the store, the relay with its outbox, the approver with its passkey panel, the board page and its writer, and `request_human` (R61 to R66, R85, R137, R145). It is not installed on any node. `reporting` and `board` still serve the `operations` tool, and they go at the cutover, after the federation threads move onto persistent rooms (`channels.md` R72 item 5).
- **The Kanban is a Trilium note tree.** The Swarm board has seven column books: Proposed, Planned, Active, Blocked, Pending review, Done and Rejected (`reporting/ledger.py` `COLUMNS`, `reporting/swarmboard.py`). Each node's reporting service keeps a ledger as the source of truth, with integer task ids per node (R106 item 1). A sweep mirrors the ledger onto the board. A failed board write leaves a dirty mark for the next sweep (`reporting/tasks.py`).
- **A second board exists.** The federation board (`board` service) holds cards between swarms, also as Trilium notes. A task linked to a federation card mirrors its state there (`tasks.py` `_mirror`). Cards for our own swarm leaked onto it (journal `50deb4fd`).
- **The queue is two columns.** `request_human` with `kind:"blocked"` moves a card to Blocked. `kind:"review"` moves it to Pending review. An urgent block also sends Tony a Discord direct message. `kind:"approval"` posts an Arbutus bundle to Slack for Tony or Ryan (`reporting/approvals.py`, `users.md` R123).
- **One click works only while the session lives.** A card shows an Agent line. It links to the session's claude.ai Remote Control page, or shows the attach command when Remote Control is off (`reporting/record.py` `_agent`). The line drops when the session ends or the task closes.
- **A drag is the only action.** Tony acts on a card by dragging it to another column. The sweep reads the drag on its next tick. A drag to Done or Rejected closes the task (`tasks.py` `_board`). A card missing from two sweeps in a row cancels its task.
- **A card starts no session.** A session claims a card when its prompt says `claim task <ref>` (`tasks.py` `hook_prompt`). Tony opens a session first and pastes that line.
- **An answer reaches the session through its prompt.** Any prompt to a blocked session moves its card back to Active (`hook_prompt`).
- **The vault runs on several nodes.** One vault runs as copies whose sync links carry no authentication, so every copy can rewrite every note (`trilium/INSTALL.md` § One document, three machines). R105 keeps Trilium on deneb only.
- **The trilium tool cannot write the board.** Its note verbs refuse any call that crosses an edge, and `note_create` takes no fixed note id. The board writer on alcor needs both (R105, `fleet.md` R87 item 3).
- **The edge may refuse the embedded panel.** The awm edge's content security policy must allow alcor's address as a frame source (R65).
- **R137 missing.** 2fa approvals reach Tony only on Discord: Approve and Deny buttons, and `/approve` to arm a burst (`awm/services/2fa/awm/twofa/notify.py`).
- **R85 partial.** alcor's authorized keys admit mizar and capella, and lyra has no key into alcor. Its firewall admits port 22 from mizar only (`fleet.md` § Gap, R98 item 1). No enrol command runs on alcor.
- Each card tracks one session. `sessions.md` § Gap, R57 and R58, holds that gap.
