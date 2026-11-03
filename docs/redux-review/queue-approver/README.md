# queue-approver

## Purpose & Contents

This doc holds the review of the task queue, the Swarm board and the approver with its action panel: how they are built today, where the build and `docs/redux/queue.md` (R60 to R66, R85, R137) and `fleet.md` (R97, R100, R105, R106) disagree, and each decision Tony makes on them. The settled decisions go into the area docs as requirements.

## As built

Surveyed read only on mizar, 2026-11-03.

- **origin/release at 266f4b29** has none of R61 to R66, R97, R106 or R137.
  - Each node's `reporting` service keeps its own ledger with integer task ids per node and job (`reporting/ledger.py`). The board is a Trilium Kanban with seven columns, a Ledger note and Older books (`reporting/swarmboard.py`). A drag is the only action.
  - `operations request_human` takes `blocked`, `review` and `approval`. An urgent block also sends a Discord DM. `approval` posts an Arbutus bundle to Slack for Tony or Ryan (`reporting/approvals.py`). `hook_prompt` moves a blocked card back to Active on Tony's next prompt (`tasks.py:466-482`).
  - 2fa approves only on Discord: buttons and `/approve` (`2fa/twofa/notify.py`).
- **The lane `feat/redux-queue`** (0fe170f5, worktree `redux-queue`) holds 5 local commits on 6d83b353, 35 behind `feat/redux`. Its 61 unit tests pass. The Trilium and browser tests did not run. Plan: `docs/redux/drafts/redux-queue-plan.md`.
  - `taskqueue` on alcor (416f8f38) is a SQLite store with the four sections, the sessions lane's verb set, an events log and `card_action`. Spawn refuses a Proposed note whose body hash differs from what the panel signed (`store.py:422-436`).
  - `queue-relay` on every node (416f8f38) forwards to alcor with an outbox, and pulls alcor's signed spawn orders into `start_other` (`relay.py:100-259`).
  - `approver` on alcor (d2823c8f) uses py_webauthn. It keeps the R65 device chain, signs Ed25519 grants and `admin_ssh` certificates, and issues the R85 enrol link (`core.py`, `cli.py`). A request carries a batch or a window of at most 12 h. A new device, a new network or an unusual rate closes a window.
  - The panel (2f98b5f7) keeps the device key in IndexedDB, takes only a card id from the board, and holds the spawn, new task, close, reject, approve, deny and new note buttons. Its 2fa strip is always shown (`panel.js`). It also serves `/panel/button` orders and the R84 `/login` grant.
  - The board (0fe170f5) is a Trilium render note with the four sections, chips, search, the sessions table and the panel frame. A writer on alcor pushes events to deneb over ETAPI with its own token (`writer.py`).
  - The plan defers removing `reporting`, the Kanban, Ledger and Older to cutover. It lists seven host changes and applies none.

## Differences from the design

- **alcor reaches deneb.** The writer and the panel tunnel both connect from alcor to deneb. `fleet.md` R87 item 3 says alcor reaches mizar only. The writer calls ETAPI directly, where R105 sends other nodes through the `trilium` tool.
- **Ryan's approvals.** The cutover drops `request_human kind:"approval"`, the Slack path for Arbutus (`users.md` R123). Nothing replaces it.
- **Dropped behaviour.** The lane drops the urgent Discord DM. A Needs you card stays there after Tony answers, until the session reports. Release moves it back on his prompt.
- **Lane values no requirement states.** The 12 h window cap, burst counts 1, 2 and 4, a 60 s expiry for a pending action (R65 item 4 says only "short"), `denied` as an outcome (R97 item 1 lists approved, expired and cancelled), and the spawn body hash check.
- **R61 item 4.** Done pages back with an Older button, not on scroll. The other sections cap at 50 cards.
- **R63 item 1.** The new note button sits in the panel, not in Proposed.
- **R60.** The lane adds about 6k lines and removes nothing until cutover.
- **R106.** Reports travel as gateway calls, not through the R98 singleton door. The relay does not outbox `approval_request`.
- **install.md R116 item 2.** No request kind exists for a plugin install or commit.
- **Not verified.** The panel's IndexedDB stays unpartitioned in the frame only if alcor's name shares the vault's site.
- **Overlap with other lanes.** `feat/redux-sessions` builds a stand-in store in `reporting` with the same verbs. Task id minting exists three times. `feat/redux-trust` copies the grant vectors twice. The sessions, harness and trust lanes edit `reporting` files that the cutover retires.
- **Doc contradictions.**
  - R64 says every action takes the passkey, and its 2026-11-03 bullet lets one approval cover a batch or a window. R65 says the panel needs nothing beyond the button.
  - TO-DISCUSS 1d and 1r and `sessions.md` R68 still name mizar as the approver.
  - `install.md` lists no approver, `taskqueue` or `queue-relay` among the core plugins, and keeps `reporting` and `board`.
  - `queue.md` § Gap from today predates R105, R106 and the lane. Its R85 line contradicts `fleet.md` R98 item 1.

## Decisions

## Follow-ups for the build
