# Queue

## Purpose & Contents

This doc holds Tony's interface to the swarm: the Swarm board, its stages, what its cards show and how they update, what happens to old cards, and how one click on a card starts a conversation with its agent. Thread 1d produces it. `goal.md` § Core loop steps 4 and 5 set its frame.

How a session closes and raises follow-up issues belongs in `sessions.md`. What holds a scope's claim belongs in `scopes.md`.

## Requirements

- **R60.** The board improves its cards, simplifies its stages and handles old session cards better. The change removes more than it adds. Tony: "add to the cards, simplify the stages, and add a better mechanism for old session cards." and "I think a lot of this can be done by just improving the cards in terms of what they show and how they are updated and actually removing things rather than adding" (2026-11-01).
- **R61.** The Swarm board is a new page built in Trilium, a command center. It replaces the Kanban. It holds four collapsible sections of card headers. Tony: "we are updating the kanban board to 4 collapsable sections of card headers (title and tags/chips, no content) with info panel on right for sleected card. The kanban is no more.", "i think actually having a new page (but still built in trillium please) is needed", "This is basically a command center" and "I really want this to be on trillium so that things are harmonized" (2026-11-01).
  1. **Proposed** holds Tony's notes for future tasks. Tony: "Need a fourth proposed section with button for new note cards that defaults to markdown for me to describe future tasks."
  2. **Needs you** holds every card that waits on Tony. A status tag on each card says why, such as planned or blocked. Tony: "3 collapseable sections. Needs you, working, done. With status tags like planned, blocked, etc."
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
  - Every login and every action takes the passkey, over Tailscale too (`fleet.md` R84). Tony: "With r84, i need a passkey even over tailscale" (2026-11-01).
  2. alcor weighs risk signals, such as a new device, a new network or an unusual rate. A risk signal asks for the passkey again. Tony: "Risk signals also seem like somwthing i want."
  3. The design works on a phone with no standing VPN link and with nothing to install. Tony: "This needs to work with phones which dont play well for constant link on a vpn like tailscale." and "Is there no built-in browser mechanism to prevent the install of the app (or executable or softare for linux, mac, windows, android, iphone... this becomes pain)?"
  4. The implementation uses existing packages for these standards. Tony: "Since this all seems like established standards, we should use existing packages when implementing".
  - deneb cannot read what passes through it. alcor opens an outbound tunnel to deneb, and deneb forwards the TLS of alcor's address down it unopened. Only alcor holds that address's private key. A CAA record limits certificates for the address to DNS validation, and only alcor holds the DNS credentials.
- **R65.** The card's action buttons are a light panel from alcor's own origin, embedded in the board page. It holds the device key and a hash chain with alcor, and needs no action from Tony beyond the button. Tony: "Whatever lets the device hold the chain locally, i dont want to have open on every prompt. It just keeps a ledger with mizar to prevent man in the middle attacks" and "Ok,  this looks good." (2026-11-01).
  1. The panel loads with the page, not at first use. Tony: "The mizar elements should also be light and load with page rather that at use to minimize performance impact."
  2. The device and alcor each keep only the latest hash. The device signs the full action message together with the latest hash. The new hash covers the previous hash, the message and the signature. alcor acts only on the exact message that the signature covers, so deneb cannot reuse a signature on another message, or a second time. Tony: "the signed message needs to be worked into the hash so that deneb cant use my signature and  replace the message" (2026-11-01), "I want a chain on device and mizar" and "the crypto ledger is essentially latest hash only. We dont need history, just need to know when it doesnt match up."
  3. alcor acks each action with its new hash, signed with alcor's key. The device sends no new action until the last one is acked. Tony: "Some automated ack mechanism prevents advancement on internet drop (device can be ahead, but not behind)."
  4. After a drop, alcor's hash must equal the device's acked hash or its pending one. alcor refuses a pending action past a short expiry, and the device drops it. Any other mismatch locks the device until the passkey.
  - Each device keeps its own chain with alcor.
  - The browser's same-origin rule keeps deneb's page code out of the panel's storage and buttons. deneb cannot sign an entry, so it cannot inject a command.
- **R85.** Tony recovers from the loss of every trusted device over ssh to alcor from capella or lyra. One command on alcor prints a one-time enrol link. The same path enrols the first device. Tony: "Losing everything, i should be able to ssh into mizar from capella and lyra" (2026-11-01).

## Decisions

- 2026-11-01: The inter-swarm board goes. Thread 1f decides how foreign agents communicate in its place. Tony: "Wr shoukd also remove the inter swarm board -> to be discussed with how foreign agents communicate".

## Gap from today

Surveyed read only on 2026-11-01, from the `feat/redux` source.

- **The Kanban is a Trilium note tree.** The Swarm board on altair has seven column books: Proposed, Planned, Active, Blocked, Pending review, Done and Rejected (`reporting/ledger.py` `COLUMNS`, `reporting/swarmboard.py`). Each node's reporting service keeps a ledger as the source of truth. A sweep mirrors the ledger onto the board. A failed board write leaves a dirty mark for the next sweep (`reporting/tasks.py`).
- **A second board exists.** The federation board (`board` service) holds cards between swarms, also as Trilium notes. A task linked to a federation card mirrors its state there (`tasks.py` `_mirror`). Cards for our own swarm leaked onto it (journal `50deb4fd`).
- **The queue is two columns.** `request_human` with `kind:"blocked"` moves a card to Blocked. `kind:"review"` moves it to Pending review. An urgent block also sends Tony a Discord direct message. Nothing joins the two columns into one list or orders them.
- **One click works only while the session lives.** A card shows an Agent line. It links to the session's claude.ai Remote Control page, or shows the attach command when Remote Control is off (`reporting/record.py` `_agent`). The line drops when the session ends or the task closes. A finished card shows its record and journal, with no way to start a session from it.
- **A drag is the only action.** Tony acts on a card by dragging it to another column. The sweep reads the drag on its next tick. A drag to Done or Rejected closes the task (`tasks.py` `_board`). No drag reaches the session. A card missing from two sweeps in a row cancels its task. The mirror keeps a hand drag over its own move.
- **A card starts no session.** A session claims a card when its prompt says `claim task <ref>`, or holds a card link with "claim" or "attach" (`tasks.py` `hook_prompt`). Tony opens a session first and pastes that line. Discord `/new-agent` starts a fresh session from free text and takes no card (`sessions.md` § Gap, R9).
- **An answer reaches the session through its prompt.** Any prompt to a blocked session moves its card back to Active (`hook_prompt`). A message on a federation card's thread wakes the assigned session with one line (`reporting/subscriber.py`).
- **No Trilium on mizar.** One vault runs as copies on several nodes. A note on any copy reaches the others within a minute. A copy's sync link carries no authentication, so every copy can rewrite every note (`trilium/INSTALL.md` § One document, three machines).
- **deneb is the public host.** deneb is a Tencent VM that serves `nexus.tony-xy-liu.com` (cloud `README.md`). It runs the federation board host (`board/INSTALL.md`) and Discord seat agents (`users.md`), and holds a copy of the vault (`auth/INSTALL.md`, `reporting/INSTALL.md`). `trilium/INSTALL.md` names sirius as the public host and the sync hub. The docs disagree.
- **The vault trusts every account.** An awm account reaches the whole vault. A writable slice lets anyone with its link create notes inside one subtree. Trilium runs each note's frontend scripts on the awm origin, as the person who reads it (`trilium/INSTALL.md` § What a shared origin costs).
- **The edge may refuse the embedded panel.** R65 embeds an alcor page in a Trilium page. The awm edge's content security policy must allow alcor's address as a frame source.
- **R85 partial.** alcor admits mizar's and capella's keys. lyra has no key into alcor. No enrol command exists.
- Each card tracks one session. `sessions.md` § Gap, R57 and R58, holds that gap.
