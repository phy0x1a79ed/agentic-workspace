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

- 2026-11-03: alcor reaches deneb only through deneb's singleton services, the `trilium` tool and the panel tunnel, which give no edge (`fleet.md` R87 item 3). Tony: "agree".
- 2026-11-03: Two tools serve the queue: `operations`, which takes in the task queue and the approver, and `trilium` for the vault. `reporting` and `board` go (`queue.md` R145, `install.md` R115 item 4). Tony: "queue and approver go to operations. From what you listed, only 2 tools are sufficient for concerns, which are trillium and operations".
- 2026-11-03: The `trilium` plugin is core, with its source in awm. Our fork publishes a tarball per release, awm pins one, and a fork change deploys by release, pin bump and awm deploy (`install.md` R115 item 4). Tony: "additional constaints on core is that the source code just lives in awm. trillium is a fork so it cant be core." and "accept if: we publish installable tarball distributions  from our fork that is used by awm changes from the local trillium project (our fork) gets a straightforward deploy route to update live services".
- 2026-11-03: An urgent request still sends Tony a Discord direct message (`queue.md` R61 item 2). Tony: "i still want urgent requests to ping me via discord dm".
- 2026-11-03: A note change, such as the board page, goes live through the `trilium` tool with no build. A fork code change runs first on the fork's dev server on mizar against a vault snapshot copy. The fork moves to mizar (`install.md` R115 item 4). Tony: "is there a hot path for trying out changes to trillium without going through a full build?" and "agree. migrate trillium to mizar".
- 2026-11-03: Tony's prompt to a session moves its card from Needs you to Working (`queue.md` R61 item 2). Tony: "agree".
- 2026-11-03: A window lasts at most 12 hours. An in-flight action, a signed press with no ack, expires after 60 seconds. Spawn acts only on the signed Proposed body. The 2fa strip arms bursts of 1, 2 or 4. History outcomes are approved, denied, expired and cancelled, and only the requesting session cancels, through `operations` (`queue.md` R64, R65, R137, `fleet.md` R97 item 1). Tony: "-> inflight action" and "otherwise accept".

## Follow-ups for the build

- Merge `taskqueue`, `queue-relay` and `approver` into one core plugin, `operations`, with one tool. Move `request_human` and the sessions lane's task verbs onto it. Drop the sessions lane's stand-in store in `reporting`, and keep one task id minter (R145).
- Remove `reporting`, `board`, the Kanban, the Ledger and the Older notes at the cutover (R60, R145).
- Make the fork's release publish a server tarball, and make `trilium/install.sh` install only the pinned tarball. Drop the build mode. Where the tarball is published waits on the fleet-deploy canonical repo question (R115 item 4).
- Move the Arbutus approval path, `request_human kind:"approval"` on Slack for Ryan, from `reporting/approvals.py` into `operations`. It stays apart from the passkey approvals (`users.md` R123, R145).
- Keep the urgent Discord DM in `operations` `request_human` (R61 item 2).
- Move the fork `projects/trilium` to mizar, as a bare repo with its worktrees, from wherever it lives now. Read altair only. Find the fork's dev server command, and check it against a snapshot copy (R115 item 4).
- Port `hook_prompt`'s move back into `operations`, so Tony's prompt moves the card to Working (R61 item 2).
- Forward approval request, get, wait and cancel through the `operations` relay, with the outbox. Make `approval_cancel` refuse any caller but the requesting session (R97 item 1, R106 item 6).
- Rename `pending` to in-flight in the approver's resume path and panel (R65 item 4).
- Make Done load older cards on scroll, and drop the 50-card cap elsewhere or page it (R61 item 4).
- Put the new note button in the Proposed section (R63 item 1).
- Route reports through the R98 singleton door (R106 item 1).
- Add a request kind for a plugin install and a commit move (`install.md` R116 item 2).
- Check that the panel's IndexedDB stays unpartitioned in the board frame on the real names, not localhost (R65).
- Keep one copy of the grant vectors across the queue and trust lanes, and one task id minter across the queue and sessions lanes.
- Rebase the lane on `feat/redux`. It is 35 commits behind.
- Fix the docs: R64's "every action takes the passkey" against its window bullet and R65's "nothing beyond the button", the mizar approver in TO-DISCUSS 1d and 1r and `sessions.md` R68, and `queue.md` § Gap from today, which predates R105, R106, R145 and the lane.
- Make the board writer call the `trilium` tool, and drop its ETAPI token (R87 item 3, R105).

## Build result

Reported by the redux-queue lane on 2026-11-03. Local commits on `feat/redux-queue` only: 59d23c89, f41612a4, e987f9b7, f6edcba3. 103 tests pass and 1 is skipped, the Chromium panel and board tests included.

- **Built.** One `operations` plugin and tool at major 2 (`awm/plugins/operations`). It holds the store, the approver, the trilium-tool writer and the door on alcor, and the relay on every node. Also built:
  - `request_human` with the urgent DM and the Arbutus path, and `hook_prompt`'s move.
  - Approvals through the outbox. `approval_cancel` takes only the calling session.
  - Reports over the R98 door.
  - In-flight in place of pending, cursor paging, and New note in Proposed.
  - The `plugin_commits` request kind, and `ops_post` for auth.
- **Open for Tony.**
  - The `trilium` tool's `note_create` takes no `note_id`, so the fixed card ids fail.
  - The `trilium` note verbs are operator only (`trilium/hub_adapter.py:573`) and refuse a call across an edge, so alcor's writer is refused on deneb.
  - The approver reads the client address from `AWM_APPROVER_FORWARDED_HEADER` while PROXY protocol is missing.
  - `order_pull` serves any caller that names a node.
  - The Chromium libraries come from conda-forge into `awm-queue`.
  - Fleet's `AWM_TRILIUM_URL` is stale.
- **Cutover gaps.** `operations` does not serve these reporting verbs: `hook_plan`, `hook_plan_approved`, `hook_todos`, `hook_session_clear`, `claim_task`, `track_task`, `board_watch`, `board_unwatch`, `swarm_*`, `board_ensure`, `convert`, `peer_list` and `peer_resolve`. The urgent-block reminder (`reporting/tasks.py:813`) is not ported. The relay needs `reflection session_of` from the harness lane.
- **The fork on altair.** `~/agentic_workspace/projects/trilium`, a bare repo with `dev` and `release` worktrees. origin is `github.com/phy0x1a79ed/Trilium`. The dev server is `pnpm server:start` on port 8080, with `TRILIUM_DATA_DIR=data` under `apps/server`. `release` holds the nightly vault snapshot commits.
- **For the cleanup pass.**
  - `reporting/approvals.py`, `reporting/config.py:160-198` and `reporting/intake.py:117` duplicate the new `operations/arbutus*.py`.
  - Two one-line JSON unix socket servers: `operations/hub_adapter.py:77` and `operations/door.py:37`.
  - Two tool call paths in one plugin: `operations/arbutus.py:108,122` against `operations/gateway.py`.
  - The payload shapes are written twice: `approver/core.py:572` and `operations/api.py:69`.
  - The test-only `operations/tests/etapi_tool.py`.
  - The trust lane's grant vector copies.
