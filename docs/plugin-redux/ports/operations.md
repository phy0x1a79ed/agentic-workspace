# Port plan: operations

## Purpose & Contents

This file holds the approved port plan for operations, from task T11 of the ops group's plan (task CWPYAtyb). It was approved on 2026-11-06 and revised on 2026-11-08 from CONTRACT.md b11adebc. It names only what changes. The Python code and its manifests are the reference for everything the port keeps. The rules that hold for every ops port are in [README.md](README.md). Operating the plugin is in `core/plugins/operations/INSTALL.md`. Later changes go in the port commits and the journal.

## Port operations, move the board writer to trilium and migrate the cards

`queue.db` on alcor is the source of truth. The vault mirrors it one way. T11 lands before T9, because the board stays down until operations lands.

Consolidations:
- **C1.** The writer, the Trilium code, `board/` and `board-install` move to trilium, which stays Python. operations holds no Trilium code. trilium renders the Ops book from `ops_list`, and the Portals note.
- **C3.** The grant code goes to awm-trust.
- **C4.** One binary serves the service, `door --node`, `enrol-link`, `devices`, `hook`, `check-state`, `downgrade-sections` and `state`.
- **C5.** ssh certificates are built in process with `ssh-key`.
- **C6.** The static assets are embedded in the binary.

Removed:
- the device actions `note_new`, `spawn`, `new_from` and the generic `order {kind,node,payload}`, with `DELIVERED` and the zotero handoff (Tony: "zotero_sync can go");
- the pages `/panel` and `/panel/button`;
- the hash chain: `/api/act`, `/api/resume`, `/api/unlock` and `/api/unlock/options`;
- the signed-read headers `x-awm-*` and the signed reads. The cookie replaces them;
- `/portal_login` and `/api/login/*`. `/signin` replaces them;
- the needs on trilium, zotero, 2fa, reflection and social. Each is a plain call that degrades.

The chain columns of `approver.db` stay in place, unread, so `state check` passes.

Store, first start, after the snapshot. Every change is additive:
- the columns `tasks.body`, `tasks.source_note` and `sessions.job`. Writes from a caller with a job record it;
- the table `ops_notes(kind, node, title, body, ts)`, one row per kind and node. `ops_post` upserts and answers `{ok}`;
- kv `vault_mark`, set from `writer_mark`;
- the sections `needs_you`, `working`, `stale` and `history`. One UPDATE rewrites `done` to `history`. `downgrade-sections` reverses it for the rollback.

Changed verbs:
1. `task_get` and `request_human` answer `card_url`, from the `card_url` template, in place of `panel_url`.
2. `approval_request` attaches to the requester's own card, which goes to Needs you with status `approval`. It opens a card only when the requester has none. Resolving the last pending request moves the card back to Working. It answers `modal_url`.
3. An intake card opens in Needs you with status `intake`.

Hidden verbs. Each refuses a session stamp and an `as_`:
1. `board_changes {after, limit}` answers `{cards, mark, more}`. `tests/fixtures/board_changes.json` pins the card shape for trilium's tests too. `approvals` maps the stored `denied` to `rejected`.
2. `board_import {cards, ops_notes, accept_gap?}` admits only the trilium plugin. It checks the task_id set against the events up to `vault_mark`, resolved through `folded_into`, and a body on every note card. It moves Proposed cards to History. On a mismatch it answers `{ok: false, missing, extra}` and writes nothing. `accept_gap` is `true` or the exact list of ids to accept. A replay answers `{ok, replay}` and writes nothing.
3. `ops_list` answers `{notes}`.
4. The emitter `operations/board` sends `{task_id, seq, ts}` at each event the store appends. Nothing is sent at boot.

Caller gate:
- `devices`, `device_revoke`, `order_pull` and `order_done` refuse every calling plugin.
- `devices` and `device_revoke` also refuse a session stamp and an `as_`.

Stale:
- operations follows `telemetry/sessions`. A `gone` ends the row, and a Working or Needs you card with no live session moves to Stale. A `live` moves a Stale card back to Working. A `renamed` sets the row's name, and the name of a card's main session becomes the card's title.
- At start and at each reconnect it reconciles against `telemetry nodes` (CONTRACT.md d5c3a3f2), matching on session_id, then on job. A listed name that differs from the row's counts as a `renamed`. Nodes the roster omits stay untouched.
- With telemetry absent, no card goes stale.

The vault API, on the approver server:
- `/signin` and `/enrol` set the cookie `__Host-awm_device`, valid 12 hours. `approver.db` keeps only its sha256, in the additive table `device_sessions`. A device revoke ends its sessions.
- Every `/api/*` route but the key, enrol and sign-in routes needs the cookie. A POST needs an `Origin` that is the approver's or in `board_origins`. CORS answers only `board_origins`, with credentials.
- The everyday actions take the cookie alone: `/api/agent`, `/api/card/{id}/close|reject`, `/api/seat/remote_control` and `/api/request/{id}/reject`. More than `rate_max` in a minute from one device are refused.
- The modal approves with a passkey whose challenge is the sha256 of the canonical request, so an assertion fits one request only. The 2fa burst and the device revoke use a single-use server nonce.
- `/signin?next=` with a live session redirects with a `portal_login` grant. The grant's return path is the bare path, because the edge refuses a query.

The `agent` order:
- The payload is `{task_id, node, note_id?}`. The relay calls reflection `start_other` with `suggest`, `remote_control`, the task and role `main`. A note gives one fixed prompt line, and no vault text reaches the agent.
- `allowed_orders.agent` defaults to alcor alone, because only a node that runs the relay pulls its orders.
- The expiry sweep sleeps until the next expiry and wakes when a request opens.

The Python edge carries one change: `portal.py` redirects to `/signin`.

Dependencies:
- The secrets and the alcor network are kept. No needs.
- trilium gains `[[needs.tool]] operations` major 2.
- One cloud-role change moves the operations unit to the plugin binary and drops the miniforge bind. The same batch adds deneb to the operations callers in `edges.yml`.

Cutover on alcor:
1. Tell plugin-redux/interview, which holds the batch before alcor's rollout.
2. Run `operations check-state` on a copy.
3. Stop the Python service. Confirm that `writer_mark` equals max(events.id).
4. Run `operations state snapshot`.
5. Tell plugin-redux/interview to resume.
6. Restart trilium on deneb. It runs `board_import` and checks the counts.
7. Compare the recorded reads by hash. Run a scratch card's write path. With Tony, run one passkey close and one 2fa burst.

Rollback:
1. Stop the unit.
2. Run `operations downgrade-sections`.
3. Revert the commits and deploy.

Keep the live `queue.db`, `approver.db` and `relay.db`. Restore only the store that `state check` proves damaged. For `approver.db`, re-enrol through `enrol-link` instead. Restore the vault from trilium's snapshot. The Python writer resumes from `writer_mark`.

Gotchas:
- A stored passkey that Rust cannot parse locks Tony out of approvals. `check-state` must pass first, and `enrol-link` over ssh is the recovery.
- Canonical JSON must be byte-equal across the browser, Python and Rust.
- Long-polls of 55 s need gateway timeouts above 60 s.
