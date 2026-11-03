# redux-queue plan

## Context

The redux replaces the Kanban with a command center page in Trilium on deneb. One task queue on alcor feeds the page. alcor authorizes each of Tony's actions through a passkey panel embedded in the page. This lane builds the queue, the approver with its panel, and the board page as core plugins. It tests them locally against a Trilium container and fakes. It deploys nothing and changes no host.

## What you said

> 2fa approvals go through discord -> that should go to the issues queue page with contant buttons in case I need to use them. I think I want the discord channel as well

> we are updating the kanban board to 4 collapsable sections of card headers (title and tags/chips, no content) with info panel on right for sleected card. The kanban is no more.

> Whatever lets the device hold the chain locally, i dont want to have open on every prompt. It just keeps a ledger with mizar to prevent man in the middle attacks

> unified task queue service on deneb rather than decentralized. -> should be on alcor and deneb is just a front

> Since this all seems like established standards, we should use existing packages when implementing

The coordinator's brief: "build and test locally against a local Trilium or a fake, and deploy nothing. Changes to Cloudflare or the edge security policy are host changes: write them up in your plan, do not apply them. Task ids come from redux-sessions; agree that interface with it."

## Issues

- I1. Each node keeps its own reporting ledger, and no single store holds every task or mints a task id that spans sessions.
- I2. The board is a seven-column Kanban that Tony drives by drag, and a sweep reads the drags back.
- I3. Blocked and review cards sit in two columns, and approval requests have no place on the board.
- I4. A card shows no table of its sessions and loses its agent link when the session ends.
- I5. A card cannot start a session, and Proposed notes cannot become tasks.
- I6. No passkey, device chain or signed action exists, so the untrusted host deneb could forge any board action.
- I7. No approver signs expiring grants, so admin logins, invites, trust raises and browser sign-ins have no approval path.
- I8. 2fa approvals reach Tony only on Discord.
- I9. No command enrols a first device or recovers from the loss of every device.
- I10. A session loses its reports while the central store is down.

## High-level goals

- G1. One queue on alcor holds every task and everything that waits on Tony.
- G2. Tony sees the swarm on one Trilium page with four sections, a search, and a detail panel for the selected card.
- G3. Every action Tony takes is his own, signed on his device, and deneb cannot forge, replay or alter it.
- G4. Approvals of every kind, 2fa included, sit in the same queue and act through the same buttons.
- G5. Tony can always get back in, even after losing every device.
- G6. Sessions keep working while alcor is unreachable.

## Acceptance criteria

- `awm/plugins/taskqueue`, `awm/plugins/queue-relay` and `awm/plugins/approver` each hold an `awm-plugin.toml` that follows the redux-core contract (draft v1). Each also holds a `run.sh` and a `service.toml` for today's Python gateway.
- The store mints task ids that match the shared test vector: the UUID `00112233-4455-6677-8899-aabbccddeeff` gives `ABEiM0RV`.
- The store serves `task_open`, `task_attach`, `task_join`, `task_for_session`, `task_sessions`, `task_report`, `task_session_end`, `task_get` and `task_list`. The names match the redux-sessions shim. `task_join` returns the main session id.
- A card sits in exactly one of `proposed`, `needs_you`, `working` and `done`. Status tags appear only in `needs_you` and `done`. Each card carries a peer chip.
- `approval_request`, `approval_get`, `approval_wait` and `approval_cancel` exist. A request card sits in `needs_you`. A decided or expired request moves to `done` with its outcome tag. A request expires 24 h after it is made.
- An approved request yields an Ed25519-signed canonical grant that `verify_grant` accepts. A changed byte fails the check. Kind `admin_ssh` also yields an ssh certificate that `ssh-keygen -L` shows with principals `admin@<node>` and the approved validity.
- The approver acts only on a message whose device signature covers it, with the chain head alcor holds. Tests show each of these is refused: a replay, a message swapped under a valid signature, a stale head, and a second action before the ack. A drop recovers by the rules of R65 item 4.
- A risk signal (new device, new network, rate) makes the approver demand a passkey assertion before it acts.
- `enrol-link` on alcor prints a one-time link. The link enrols a passkey and a device key once, then stops working.
- The panel always shows the 2fa strip: arm a burst, approve all, and Approve and Deny on each held push. Each press reaches the 2fa verbs through a signed action. The Discord path stays unchanged.
- The relay keeps reports in a local outbox while the store is down. It replays them in order when the store returns.
- Against a local Trilium container, the board writer creates the page and the card notes, and the page renders four sections with search and Done paging. A Playwright run with a virtual authenticator enrols a device, signs an action and sees the card move.
- The plan's Callouts list every host change the work needs, with nothing applied.

## Tasks

- T1. Write and commit this plan (G1–G6).
- T2. Set up the plugin env and a local Trilium container (G2).
- T3. Build the task store on alcor (G1, G4).
- T4. Build the relay with its outbox and spawn orders (G1, G6).
- T5. Commit a checkpoint.
- T6. Compact.
- T7. Build the approver core: passkeys, device chain, risk, grants and enrolment (G3, G4, G5).
- T8. Build the action panel with its 2fa strip (G3, G4).
- T9. Commit a checkpoint.
- T10. Compact.
- T11. Build the board page and its Trilium writer (G2).
- T12. Test end to end in a browser with a virtual authenticator (G2, G3, G4).
- T13. Commit a checkpoint.
- T14. Debrief.

## Approach by task

### T1. Write and commit this plan

Write this file and commit it on `feat/redux-queue`. Message redux-sessions, redux-trust and redux-core about the interfaces settled in it.

Gotchas: The coordinator owns the area docs. Put proposed doc changes in Callouts, never in `docs/redux/*.md`.

### T2. Set up the plugin env and a local Trilium container

Create one mamba env, `awm-queue`, for the three plugins (R115 item 3). It holds `webauthn` (py_webauthn), `cryptography`, `fastapi`, `uvicorn`, `httpx`, `pytest` and `playwright` with its Chromium. Write a script, `awm/plugins/taskqueue/tests/trilium-up.sh`, that starts `triliumnext/trilium` under rootless Docker on a loopback port. It sets the password through the setup call and prints an ETAPI token. The tests use it, and nothing else touches it.

Gotchas: The container is mine and lives in rootless Docker, so it changes no host. Pin the image by digest. Trilium's first-run setup differs between versions, so script it against the pinned digest only.

### T3. Build the task store on alcor

The store is plugin `taskqueue`, tool `queue-store`, `tier = "off"`, placed on alcor. It keeps SQLite with WAL. Its tables are `tasks`, `sessions`, `events`, `approvals`, `spawn_orders` and `devices_chain`. The approver owns the chain table but shares the file. A card is a task row with `section`, `status`, `peer`, `title`, `body_ref` and timestamps. Sections follow R61: `proposed`, `needs_you` (status planned, blocked, review or approval), `working` (no status) and `done` (status done, rejected, approved, denied, expired or cancelled).

The verbs are the redux-sessions set plus `task_report`, `task_session_end`, `task_get`, `task_list(section, q, before, limit)` and `note_new`. Mutating verbs that only Tony may call (`card_action`) take an action the approver verified. The store checks a shared secret that only the approver holds, so an agent cannot call them through the gateway. Each change appends an `events` row with a monotonic id. The board writer and the relay read the changes from it.

Gotchas: A card's section and tags change only through the store (R66), so the writer never reads Trilium back. `task_join` folds the joiner's own card into the target and returns `main_session_id`. The redux-sessions side sends the notice, because the singleton door carries no reflection (R104 item 4). Keep the memory small for alcor's 4 GB cap (R99 item 3): stdlib `sqlite3`, no ORM.

### T4. Build the relay with its outbox and spawn orders

The relay is plugin `queue-relay`, tool `queue`, `tier = "on"`, on every node. It serves the session-facing verbs and forwards each one to `queue-store` on alcor through the gateway. It names the tool by UUID through `AWM_RESOLVED_TOOLS`. A write that fails for transport reasons goes into a local SQLite outbox with a sequence number. The relay answers the session at once. A background loop replays the outbox in order when alcor answers again. A read while alcor is down answers from the relay's last copy and says how stale it is.

The relay also pulls spawn orders for its own node. A spawn order is a record the store wrote after the approver verified Tony's spawn action, signed with alcor's grant key. The relay checks the signature, starts the session through `reflection start_other` with the task id, and reports back. alcor never acts on another node (R97).

Gotchas: `task_open` while alcor is down needs an id at once. The relay mints it locally with the same function, and the store accepts a client-minted id when it is unused. Replay keeps order per node only, which is enough because each session reports from one node. A spawn order is single use: the relay records its id before it starts the session.

### T5. Commit a checkpoint

Run the store and relay tests. Commit them with the manifests. Message redux-sessions that the verbs exist.

Gotchas: none beyond a green test run.

### T6. Compact

Handoff: the plan file is on disk, and the store and relay are committed. The env `awm-queue` and the Trilium script exist. The todo list says T7 is next. The siblings' interface replies are in this plan's Callouts. Run `reflection(verb="compact", args={followup:"continue with T7"})`.

### T7. Build the approver core

The approver is plugin `approver`, tool `approver`, `tier = "off"`, placed on alcor. It runs two servers. The gateway service serves `approval_request`, `approval_get`, `approval_wait`, `approval_cancel`, `devices` and `device_revoke`. Its own HTTPS server, on alcor's own name, serves the panel and its API (R64, R65). Passkeys use py_webauthn for registration and assertion (R64 item 4).

- **Enrolment.** `enrol-link` is a command that talks to the approver over a local unix socket. It prints a one-time link with a 15-minute expiry. The link page registers a passkey and a non-extractable WebCrypto P-256 device key together. A later device enrols from an approval card that a trusted device approves (R64 item 1).
- **Chain (R65).** The message is canonical JSON `{device, seq, prev, action, args, ts}`. The device signs it with its device key. The new head is `sha256(prev || sha256(message) || signature)`. alcor acts only if `prev` equals its head and the signature verifies. It stores the new head and the last ack, then returns `{head, sig}` signed with alcor's P-256 key. The browser checks that with WebCrypto. On a reconnect, alcor's head must equal the device's acked head or its pending head. If it equals the pending head, alcor resends the stored ack. If it equals the acked head and the pending message is less than 60 s old, alcor takes the pending message. Any other state locks the device until a passkey assertion, which starts a fresh chain from a head alcor issues.
- **Passkey per request (R64, decided 2026-11-03).** Every approval request takes one passkey assertion. Its challenge is `sha256(head || sha256(canonical request))`, so it binds the chain head and the exact request. A request carries either a batch of actions, applied at once, or a window: a scope of action kinds, card ids (or any) and a length of up to 12 h. Inside an open window the device key signs each in-scope action on the chain with no prompt. An out-of-scope action needs a new request. A risk signal closes the window at once: a new device, a new network (client address), or more than N actions per minute. Tony: "Pass key on every action, but the discussion outcome was to let passkey approval request contain a batch of actions and/or open a window. So still approve on every request but the scope of requests can be large so that i dont have to keep approving".
- **Grants.** Approve and deny are actions like any other. Approve signs a canonical grant `{grant_id, request_id, kind, node, subject, payload_sha256, not_before, not_after, approved_by_device}` with Ed25519. Kind `admin_ssh` also signs an ssh user certificate with `ssh-keygen -s`. The certificate takes the payload's public key, principals `admin@<node>` for each node in the payload, the approved minutes, and the grant id as key id. `grantlib/verify_grant.py` is pure and ships `test_vectors.json` for redux-trust and the Rust side.
- **Dispatch.** A verified action becomes one call: a `queue-store` card action, a 2fa verb, or a grant. The approver is the only caller that holds the store's action secret.

Gotchas: A WebCrypto ECDSA signature is IEEE P1363 (r||s), not DER. Convert it before `cryptography` verifies it. The panel's origin must equal the WebAuthn RP ID's domain, so the test uses `localhost`. The client address arrives only if deneb's TLS forward sends PROXY protocol (Callouts). Without it, the network signal reads the tunnel and stays quiet, so the code must treat a missing address as unknown, never as trusted.

### T8. Build the action panel with its 2fa strip

The panel is one small static page with one script and no framework. It loads with the board page, not at first use (R65 item 1). It holds the device key in its own origin's IndexedDB and the acked and pending heads. It listens for `postMessage` from the board page for the selected card id. It then fetches the card from alcor itself and shows only alcor's copy, so deneb cannot change what Tony signs. The buttons follow R63: new note, spawn agent with a peer dropdown, new task from a card, close, reject, approve and deny. A Proposed note's body comes from the board page. The panel shows its first lines and signs its sha256. The store reads the body from Trilium and refuses the spawn when the hash differs.

The 2fa strip sits at the top at all times (R137). It shows each device with buttons to arm a burst of 1, 2 or 4, to approve all for 5 minutes, and Approve and Deny on each held push. The approver fills it from the 2fa verbs on alcor. Each press is a signed action. The Discord listener in the 2fa service stays as it is.

Gotchas: `postMessage` input is untrusted. Check `event.origin` against the configured board origin, and accept only a card id. The frame needs `allow="publickey-credentials-get; publickey-credentials-create"` for WebAuthn inside an iframe. Chrome and Safari also need the frame's origin in the parent's `frame-src` (Callouts).

### T9. Commit a checkpoint

Run the approver tests. Commit. Message redux-trust that `verify_grant` and the test vectors are in.

Gotchas: none beyond a green test run.

### T10. Compact

Handoff: the approver and the panel are committed, and their tests pass. T11 is next. Note any open question to the coordinator and its default in Callouts. Run `reflection(verb="compact", args={followup:"continue with T11"})`.

### T11. Build the board page and its Trilium writer

The writer is a loop in the `taskqueue` service. It reads `events` after its last mark and upserts one note per card through ETAPI on deneb. The card notes sit under a `Swarm` root labelled `#swarmBoard`. Each carries the labels `#swarmCard`, `#taskId`, `#section`, `#status`, `#peer`, `#updatedAt` and `#doneAt`. A child note `record` holds the sessions table as JSON. The writer writes the board root's `#queueHeartbeat` label each minute, so the page can show how long alcor has been unreachable (R106 item 6). A Proposed note's body belongs to Tony. The writer never writes it.

The page is a Trilium render note. Its HTML and frontend script live in `awm/plugins/taskqueue/board/`, and `board-install` upserts them. The script lists cards with `api.searchForNotes` by section and draws four collapsible sections of headers (title, status tag, peer chip). Search runs a Trilium search over the cards. Done pages back on `#doneAt` as Tony scrolls. A click selects a card and fills the right panel with the sessions table (status and Remote Control links). The script also tells the embedded approver frame which card is selected.

Gotchas: The Ledger and Older notes go (R66), so the writer must not recreate them. Trilium search has no offset, so Done pages by `#doneAt < cursor`. Every node except alcor reaches Trilium only through the `trilium` tool (R105). The writer runs on alcor, so it needs a deneb ETAPI token as a secret in `[needs]`.

### T12. Test end to end in a browser with a virtual authenticator

Run the store, the approver and a fake 2fa locally on loopback ports. Point the writer at the Trilium container. In Playwright, add a CDP virtual authenticator. Then enrol through an `enrol-link` link, open the panel in a test page that frames it as the board does, and check each flow: new note, spawn (a fake relay picks up the order), close, approve a request (check the grant), and 2fa approve. Then cut the network mid-action and check the R65 item 4 recovery. Separately, load the board page in Trilium and check its four sections and search.

Gotchas: A virtual authenticator needs `hasUserVerification` and `isUserVerified` set, or py_webauthn refuses it. Run Chromium headless with `--no-sandbox` inside the env, because rootless containers and the host sandbox differ.

### T13. Commit a checkpoint

Commit the end-to-end tests and the page. Report to the coordinator.

Gotchas: none.

### T14. Debrief

Run the `debrief` skill. Journal the lane's state. Send the coordinator one line. Stop.

## Callouts

**Passkey scope, settled.** Tony decided on 2026-11-03: a passkey on every request, and a request may carry a batch or open a window (queue.md R64, feat/redux 864a195f). T7 builds that. Nothing switches it off.

**Panel action interface for other pages (sent to redux-fleet for the Zotero Sync now button).** A page embeds `/panel/button?kind=<k>&node=<n>&label=<l>` from the approver origin. A press signs `{action:"order", args:{kind, node, payload}}`. alcor signs an order `{order_id, kind, node, payload, not_after}` in the grant envelope. The target pulls it with `approver order_pull(node, kinds, wait_s)`, checks it with `verify_order`, and reports with `order_done`. The config `allowed_orders` names which kinds go to which nodes. Spawn and new-from use the same orders, and `queue-relay` pulls them.

**Interfaces agreed tonight.**

- redux-sessions: the task id format is theirs (8 chars of URL-safe base64 of a UUID4). The queue mints with its own copy and pins the vector `ABEiM0RV`. The calls are `task_open`, `task_attach`, `task_join`, `task_for_session` and `task_sessions`, with these names in both backends. Their ledger-backed shim lives in `reporting` until cutover.
- redux-trust: the approver signs grants and `admin_ssh` certificates. alcor holds the user CA. redux-trust builds the callers, the node-side checks and `TrustedUserCAKeys`. The grant JSON is canonical (sorted keys, no spaces, integer times). The envelope is `{grant, sig, key_id}`.
- redux-core: the manifest is `awm-plugin.toml` at `awm/plugins/<name>/`. The names `approver` and `queue-store` are on the reserved list.
- redux-trust, later: request kind `vault_hold` (R105 item 3) needs only a card and a decision. `approval_get` returns the admin certificate at `ssh_cert`. An `admin_ssh` grant body carries `nodes` beside `node`. trust's Python and Rust checks pass the 12 cases in `grantlib/test_vectors.json` (feat/redux-trust 677a3604).
- redux-trust and redux-fleet, portal login (fleet.md R84): the portal sends Tony to `/login?site=&node=&return=` on the approver origin. One passkey there yields a 60-second grant of kind `portal_login`, bound to the site and the return URL, in the URL fragment of `return`. The portal checks it with `verify_grant` and sets its own session. fleet owns the portal side and the removal of the password form. auth keeps no user table for passkeys.
- redux-fleet: the Zotero Sync now puller long-polls `order_pull(node, ["zotero_sync"], 55)` (feat/redux-fleet 58109656). The deneb role takes the approver origin as a config value.

**Host changes to apply in the fleet sweep (none applied).**

1. Cloudflare DNS: make alcor's panel name (for example `approve.tony-xy-liu.com`) DNS only, not proxied, so deneb can forward its TLS unopened (gap F11). Add a CAA record `0 issue "letsencrypt.org; validationmethods=dns-01"` for that name. Only alcor holds the DNS token that can answer the challenge (R64).
2. deneb's edge: add an SNI route that passes the panel name's TLS through, unterminated, down alcor's outbound tunnel. Send PROXY protocol v2, so alcor sees the client address for its risk signal.
3. deneb's content security policy for the vault origin: redux-fleet reports that nginx, awm and Trilium send no CSP on deneb today, so nothing blocks the frame. If a CSP is added later, it must list the panel origin in `frame-src`. Allow `publickey-credentials-get` and `publickey-credentials-create` for it in `Permissions-Policy` (gap Q11).
4. The approver's own response headers: `frame-ancestors` names only the vault origin, so no other site can frame the panel.
5. alcor: an outbound tunnel unit to deneb for the panel name, and the approver's own user and unix socket. The socket's group lets the admin path run `enrol-link`.
6. lyra: an ssh key into alcor for recovery (R85, gap Q13), with alcor's firewall admitting it (gap F2).
7. deneb: an ETAPI token for the writer on alcor, held as a secret on alcor.

**Gaps left in the approver.**

- PROXY protocol: uvicorn cannot read it. Until a front on alcor turns PROXY v2 into a header, the approver reads the client address from `AWM_APPROVER_FORWARDED_HEADER` on loopback connections only. Without that header the address is unknown, and a window opened from an unknown address has no network check. The panel says so beside the window.
- A Proposed card's body hash, closed in T11: the store reads the note again through ETAPI when Tony spawns it and refuses the spawn when the sha256 differs from the one the panel signed. The spawn order carries the checked body, so a relay never reads Trilium. The check still trusts deneb at that moment. It catches an edit made after Tony looked, not a vault that lies twice.
- `order_pull` serves any caller that names a node. The orders are signed and only the named node acts on them, so a wrong caller learns the payload and nothing more. The redux gateway's caller identity can close this later.
- The browser test runs Chromium with libraries from conda-forge in the `awm-queue` env (`nss`, `atk`, `libgbm` and others), because the host lacks them and installing them would be a host change.

**The board in Trilium (T11).**

- The page files live in `awm/plugins/taskqueue/awm/taskqueue/board/`, not `awm/plugins/taskqueue/board/`, so an installed package carries them. `awm-taskqueue board-install` upserts them. Run it again after each change to them.
- Each card note's id is `swc` plus the task id in hex, and its record child is `swr` plus the same. A re-run after a crash finds the same notes, and no lookup table is needed.
- The search is `note.title *=*` or an exact task id, peer or status. A bare quoted phrase in Trilium is a full-text search, and it matched nearly every card.
- Tested against TriliumNext 0.106.1 in the local container. deneb's version is not checked. The page uses only `api.searchForNotes`, `getLabelValue`, `getChildNotes` and `getBlob`, and render notes have run a child frontend script the same way for several releases.
- The browser test opens Trilium with `?extraWindow=1`, which opens one tab and restores none. Otherwise Trilium reopens every tab of earlier runs, and the test times out.
- The writer runs on alcor and needs `AWM_TRILIUM_URL` (deneb's vault as alcor reaches it) beside the `AWM_TRILIUM_ETAPI_TOKEN` secret. redux-fleet was asked to add it, and to add `peers` to the approver config.

**Later from siblings.**

- redux-sessions (feat/redux-sessions b3a75403): `start_other` takes `task_id` and `role`. The relay passes `task_id` and `role: "main"`.
- redux-core (feat/redux-core 6d8cc642): the Rust gateway runs end to end, and contract v1.5 adds a service token on register from `AWM_HUB_TOKEN_FILE`. The approver and the task store have not run under it yet. Do that in the sweep, after the adapter on feat/redux-core is merged.

**Proposed doc changes (for the coordinator).**

- `queue.md` § Gap: once the plugins land, R61 to R66, R137 and R85 move from missing to built, pending deploy.
- `fleet.md` R106: item 6's outbox lives in the `queue-relay` plugin on every node.
- `fleet.md` R97: spawn on another peer works by a signed spawn order that the target's relay pulls, so alcor never acts on another node.

**Left for cutover, not tonight.** The `reporting` service, its Kanban sweep, the Ledger and Older notes, and the `operations` verbs go once the new plugins run (gaps Q1 to Q6). The federation board (Q2) belongs to the channels lane's rooms.
