# Port plan: operations (board writer moves to trilium; card migration)

## Purpose & Contents

This file holds the approved port plan for operations (board writer moves to trilium; card migration), from the ops group's plan (task CWPYAtyb). Tony approved it on 2026-11-06. It is the plan as approved: later changes go in the port commits and the journal.

## Rules common to every ops port

- Each port commit adds `core/plugins/<name>/` and deletes the Python folder in the same commit. That folder holds `awm-plugin.toml`, the crate `awm-plugin-<name>` with binary `<name>`, and any pages.
- Each port removes the plugin's rows from `scripts/run-tests.sh` and adds, in the same commit, a `run.sh` that is one line, `exec` of the binary, and the binary's `[artifacts]` pin line in `deploy.toml`, in the pin format of PLAN.md T2.
- Build and test only through `scripts/rust-build.sh` with `AWM_CARGO_TARGET_DIR=$HOME/.cache/awm-target/port-ops`.
- Submit with `scripts/land.sh submit --brief <file> --restart <plugin>`. Then run the live check on mizar and on one other node.
- SQLite uses `rusqlite` with `bundled`, through the knowledge group's store crate (`awm-store`) when it has landed. Otherwise the plugin opens the database directly with the same path rule and switches to the store crate later.
- The SDK needs listed per plugin go to plugin-redux/interview. No plugin builds a private copy.

## Plan

**What the code shows.**
- `queue.db` on alcor is already the source of truth for cards, sessions, messages and events. The vault is a one-way mirror that operations' board writer keeps.
- Only two things live in the vault alone: Tony's Markdown bodies on Proposed note cards, and the Ops book notices. The migration imports those two and checks the card count.
- operations runs only on alcor, as the external singleton `awm-plugin@operations.service`, user `awm-operations`. Every other node excludes it and reaches alcor through the `awm port` singleton door.

**Replaces.**
- `awm/plugins/operations/awm/operations/`: 4266 src. It includes api, hub_adapter, front 534, relay, store 550, home, door, writer, board, trilium, gateway, ids, cli, arbutus, and approver/{core 872, server, keys, passkeys, chain}.
- `approver/static` and `board/`: 837 lines.
- `hooks/`: 194 lines.
- `tests/`: 2581 lines.

`writer.py`, `board.py`, `trilium.py`, `board/`, the `board-install` CLI, test_writer, test_board_browser and trilium-up.sh move to the trilium plugin, which stays Python (C1). grantlib stays for zotero.

**Where each part runs.** The Rust binary keeps today's selection logic and environment variables:
- Every node that runs the service: the front, the relay with its outbox `relay.db`, the flusher every 10 s, Arbutus (unless `AWM_APPROVAL_CHANNEL` is empty), and the spawn-order loop (when `AWM_APPROVER_GRANT_PUBKEY` is set).
- The home node (`AWM_OPERATIONS_HOME`, default alcor): the store, the approver with its keys and its HTTP origin on `127.0.0.1:8443`, the sweep every 60 s, and the door socket.
- Any other node: the DoorClient over `AWM_OPERATIONS_DOOR`.

**Tool surface.** Tool `operations`, major 2, tier on, implements `operations@2`. HIDDEN means surfaces cli and http. A `*` marks a required param.

| Verb | Params | Surfaces |
|---|---|---|
| request_human | kind, reason, summary, urgent:boolean, bundle, title | all |
| task_open | title*, session_id, peer, tags:array, name, rc_url | HIDDEN |
| task_attach | task_id*, session_id*, role, name, rc_url | HIDDEN |
| task_join | task_id*, session_id | all |
| task_report | task_id, session_id, state*, reason, rc_url | all |
| task_session_end | task_id*, session_id* | HIDDEN |
| task_for_session | session_id | all |
| task_sessions | task_id* | all |
| task_get | task_id* | all |
| task_list | section, q, before:number, limit:integer | all |
| hook_prompt | prompt | HIDDEN |
| hook_plan | session_id*, plan_path, plan_text* | HIDDEN |
| hook_plan_approved | session_id*, plan_text | HIDDEN |
| hook_todos | session_id*, items:array* | HIDDEN |
| hook_session_clear | new_session_id | HIDDEN |
| claim_task | task_id*, session_id | all |
| track_task | title, session_id | all |
| swarm_message_post | card_id*, body*, session_id | all |
| swarm_messages | card_id*, after:integer | all |
| intake_post | board_card*, sender*, title, body, reply_to | HIDDEN |
| intake_message | board_card*, sender*, body | HIDDEN |
| approval_request | kind*, node, reason*, minutes:integer, payload:object | all |
| approval_get | request_id* | all |
| approval_wait | request_id*, wait_s:number | all |
| approval_cancel | request_id* | all |
| order_pull | node, kinds:array, wait_s:number | all |
| order_done | order_id*, ok:boolean*, note | all |
| keys | none | all |
| status | none | all |
| ops_post | kind*, node*, title*, body* | HIDDEN |
| devices | none | HIDDEN |
| device_revoke | device_id* | HIDDEN |

Other surfaces:
- Subscription: social `message`, for Arbutus.
- The approver's own HTTP origin on alcor, kept byte for byte:
  - Pages: `/panel`, `/panel/button`, `/enrol`, `/portal_login`.
  - Static assets.
  - Open API: `/api/keys`, `/api/enrol/*`, `/api/passkey/options`, `/api/act`, `/api/resume`, `/api/unlock*`, `/api/login/*`.
  - Device-signed reads: `/api/card/{id}`, `/api/request/{id}`, `/api/twofa`, `/api/windows`, `/api/devices`, `/api/order/{id}`.
  - The same CSP, Permissions-Policy and no-store headers, and the same error-code-to-status map.
- The door socket's one-line JSON protocol, with its peer-uid gate and mode 0660.
- The CLI subcommands `enrol-link`, `devices` and `door --node`.

Interface answers for other groups:
- `task_for_session` and `task_attach` keep their shapes at major 2 (harness).
- `request_human` accepts a service caller with no session, kinds `blocked` and `review` (comms). Each such call opens a new card titled by `title`, else the reason. The answer carries `task_id`, so the caller can later `task_report` it. Kind `approval` needs a `bundle`.

**Changes to the surface.**
1. Three new hidden verbs for the trilium plugin. They refuse any call that carries a session stamp. `card_body_set` and `board_import` also refuse any calling plugin other than `trilium`, which the gateway names from the service token.
   - `board_changes {after:integer, limit:integer ≤ 500}` returns `{changes:[{id, task_id, kind, ts}], last}`, the event feed the writer reads in-process today.
   - `card_body_set {task_id*, body*}` writes Tony's Markdown. It works only on a note card in Proposed, caps the body at 100,000 characters and appends a `body` event.
   - `board_import {cards:array*, ops:array*, vault_cards:integer*, accept_gap:boolean}` is the one-shot migration.

   The spawn body check (R65 item 2) compares the spawn action's `body_sha256` against `tasks.body`. trilium calls `card_body_set` on every change to a Proposed note. A press made right after an edit, before the sync arrives, therefore fails the check and is refused. It never runs on a stale body.
2. A fourth new hidden verb, `ops_list {}`, returns the stored Ops notices.
3. `task_get`'s `task` gains `body`, a string or null.
4. `ops_post` stores the notice in operations. Its answer is `{ok, kind, node, changed}` and drops `note_id`, which no caller reads. It keeps accepting a plugin call that carries no `as`, as scopes sends it.
5. The CLI subcommand `board-install` moves to trilium.
6. The Claude Code hook moves to cch, which takes over its own hook rows (harness). The `hook_*` verbs stay as the interface cch calls. cch's hook must keep the operations hook's behaviour:
   - mirror `blocked` into the `~/.claude/awm-operations/<sid>.blocked` marker;
   - print `notice` as `additionalContext`;
   - cap `plan_text` at 20,000 characters;
   - exit 0 on every path.

   operations keeps its hook and the `deploy-hooks.sh` rows until cch's port replaces them, so the settings never point at a missing script. If cch's port has not landed by T11, the hook becomes the `operations hook` subcommand of the binary, with the same behaviour. It sends the hook's caller pid as the CLI does. `awm-operations-settings.json` moves to `core/plugins/operations/hooks/` and points at the subcommand, and the rows follow it. No Python stays. Whichever of the two ports lands second deletes the operations hook and its rows. If cch's port has landed first, T11 ships no hook.

**State.** Every default path and override variable is kept. On alcor, HOME is `/var/lib/awm/operations`.
- `queue.db`: tasks, sessions, events, applied_ops, messages and kv.
- `relay.db`: outbox, dead, cache, seen_orders and meta.
- `approver/approver.db`: devices, credentials (COSE public key, sign_count), enrol_links, reg_challenges, requests, windows, action_log, orders and logins.
- `approver/keys/`: `grant_ed25519.pem`, `ack_p256.pem` and the `ssh_user_ca` pair.
- `config.json`. Its `board_peer` and `board_parent` fields become unused.
- The door socket.
- The Arbutus pending dir and bundles.

Migration on the first Rust start:
1. Back up `queue.db` with the sqlite backup API.
2. Add `tasks.body`.
3. Create `ops_notes(kind, node, title, body, changed)`.
4. Set kv `vault_mark` to the current `writer_mark`.

The other databases and the keys are read in place with no change.

**Card migration (fc14982b).**
- trilium reads its own vault: every `#swarmCard` note with Tony's bodies (note cards, and card notes without `#readOnly`), and every Ops book note. It sends one `board_import` call.
- operations checks, in one transaction:
  - `vault_cards` equals the number of cards sent;
  - the card task_ids equal the expected set, which is the task_ids of events with id ≤ `vault_mark`, resolved through `folded_into` to their root (the writer deleted folded notes);
  - every note card received a body.
- On success it writes the bodies and the notes, records kv `board_import` with its counts, and answers them. A replay of the same content returns the first result. Different content is refused.
- On a mismatch it writes nothing and answers the missing and extra ids. trilium then calls `request_human` (kind `blocked`) with both lists. Tony either fixes the vault, after which trilium retries the import, or accepts the gap. Accepting the gap is `board_import` with `accept_gap:true` and the same lists, which imports what matches and records the gap in kv `board_import`.
- trilium keeps the vault board as it is, unrendered, until the import succeeds or the gap is accepted. The board never goes blank, and a mismatch always ends in a card on Tony's board that names the next step.

This keeps operations free of trilium code, moves no file across nodes, keeps trilium the only vault reader (R145), and makes the count check atomic.

**Dependencies.**
- `secrets = ["AWM_OPERATIONS_DOOR_KEY", "AWM_APPROVER_GRANT_PUBKEY"]` and `network = ["alcor"]`, both kept.
- No `[[needs.tool]]`. The queue, the approvals and the passkey actions all work without reflection, social, 2fa or zotero. A hard need on reflection would let one reflection fault take down Tony's approvals.
- Plain calls:

| Call | Used for |
|---|---|
| reflection `session_of` | finding the calling session |
| reflection `message` | join and intake notices |
| reflection `start_other` | the order loop, timeout 120 s |
| social `send` | urgent DMs, the reminder, Arbutus posts |
| social `message` subscription | Arbutus |
| 2fa `devices`, `pending`, `burst`, `approve_all`, `approve`, `deny` | the panel strip |
| zotero `order` | Sync now |

- A failure is classed as today: a transport error, 5xx, 404, 408 or 429 means unreachable. Any other 4xx means refused.

**Shared crates.**
- awm-trust (T1): canonical, digest, key_id, load_public_key, `verify_order`, `sign_envelope` and `b64u`.
- WebAuthn and P-256 stay in the operations crate, the only user.
- An R9 session-id `normalise` belongs in awm-proto or the SDK.

**Rust crates added.**
- `rusqlite` with `bundled`.
- `p256` (ecdsa, pkcs8, pem).
- `ed25519-dalek`, already in the lockfile.
- WebAuthn verification: `webauthn_rp` if it verifies against a stored COSE key and sign count. Otherwise `coset`, `ciborium` and `rsa` for RS256. It covers registration (`none` and packed self-attestation) and assertion with py_webauthn's defaults. `webauthn-rs` is ruled out because it uses openssl.
- `ssh-key`: the CA and user certificates replace the `ssh-keygen` subprocess, keeping its default extensions, serial and key id.
- `sha2`, `hex`, `base64`, `rand`, `subtle`, `url` and `regex`.
- `chromiumoxide`, dev only, for an ignored browser test.

**Consolidations.**
- C1. The store is the only card state. The board writer and the Trilium note code move to the trilium plugin. trilium:
  - renders from `board_changes`, `task_get` and `swarm_messages`;
  - syncs Tony's edits through `card_body_set`;
  - runs the import once;
  - adds `[[needs.tool]] operations` major 2.
  The alcor writer gate on trilium's door goes.
- C2. Ops notices live in operations until telemetry's Ops page (fleet.md R147) replaces the Ops book.
- C3. Grant code goes to awm-trust.
- C4. One binary serves the service, `door --node`, `enrol-link` and `devices`.
- C5. ssh certificates are built in-process.
- C6. The static assets are embedded in the binary, so the sandboxed unit needs no read path into the tree.
- C7. The hook leaves for cch (harness), once cch's port replaces it. Until then it is a subcommand of the one binary.

**External units** (cross-group: comms, knowledge). Several plugins run as external systemd units that call `bash run.sh` from `singleton_service_dirs`:
- operations, social and browser on alcor;
- relay on deneb, whose plugin is channel;
- awm-journal on mizar, alcor and capella.

One cloud-role change gives them all the same shape:
- The drop-in's `ExecStart` runs the plugin binary at the stable path the PLAN.md T2 pin placement names.
- `WorkingDirectory` becomes the plugin folder `core/plugins/<plugin>`: `core/plugins/channel` for relay, `core/plugins/journal` for journal.
- `BindReadOnlyPaths` exposes that binary.
- The role change is generic over the plugin name, so each unit switches when its own port lands.

Other groups wait on the role change, so it does not wait for this task. It lands right after T1, as its own cloud commit. Units not yet ported keep `bash run.sh` until their own port switches them. This task then switches the operations unit. comms' channel land (relay included) and knowledge's journal land wait on the role change. Each of them switches its own unit. Knowledge coordinates the journal unit with this task.

**SDK needs.**
- `Caller` exposes the raw `_caller_pid`, `_caller_session_id`, the `_caller_session` claim, `as_`, the lane and the calling plugin's name.
- A page usage event.
- `hub.call` errors carry the status and body, and a per-call timeout reaches 120 s.
- `hub.subscribe` takes a topic of another plugin, with `peer`, under supervised reconnect.
- Secrets load from `$CREDENTIALS_DIRECTORY/secrets.env`.
- Verbs may run 60 s or more with cancel honoured.
- R9 `normalise`.
- The plugin may bind its own axum listener for the approver origin.

**Test plan.**
1. Port store (12), relay (9), front (19), door (6), server (2) and arbutus (14) tests.
2. Port approver (32) with a Rust soft authenticator: ES256, fmt none, P-256 device keys with P1363 signatures.
3. test_hook (16) ports to the `operations hook` subcommand while operations still carries the hook, and leaves with it. test_writer and test_board_browser move to trilium.
4. New tests:
   - `board_import` match, missing, extra, replay, conflict and `accept_gap`;
   - the scope of `card_body_set`, and the refusal of both write verbs from any plugin other than trilium;
   - a spawn press against a body changed after the press is refused;
   - `ops_list`;
   - the schema migration on a copy of a Python-made `queue.db`;
   - canonical JSON vectors, including non-ASCII and nested cases;
   - ES256, EdDSA and RS256 COSE parsing.
5. Pre-flight: a `check-state` subcommand runs against a copy of `/var/lib/awm/operations`. It opens the three databases, parses every stored credential's COSE key, loads the keys, and compares `writer_mark` with max(events.id).
6. Live parity on alcor.
   - Before cutover, record: describe, status, keys, `task_list` (working and done), `task_get` on two cards, `swarm_messages`, `approval_get`, `task_for_session`, `/api/keys`, the `/panel` headers and `devices`.
   - After cutover, repeat each read and expect equality, plus `body` and the four new hidden verbs.
   - Then a scratch card's write path: `track_task`, a message, `task_report` review and done, an approval request with wait and cancel, and `ops_post` with `ops_list`.
7. With Tony, through `request_human` review: a passkey close on `/panel` and one 2fa strip refresh.
8. Migration: `board_import`'s `cards` equals trilium's `#swarmCard` count, and the vault board shows every card.

**Landing.**
1. Prerequisites: T1 and the SDK.
2. One commit: add `core/plugins/operations` and delete `awm/plugins/operations`, with the trilium-bound files moved in the trilium commit of the same batch. Add the pin. Unless cch's port has already replaced the hook, point the `deploy-hooks.sh` rows at the `operations hook` subcommand (change 6).
3. In the same batch, a trilium commit in Python:
   - the renderer from `writer.py`;
   - body sync;
   - the import step, gated on kv `board_import`;
   - `board-install`;
   - the operations need;
   - removal of the writer door gate.
4. Cloud project:
   - add deneb to the operations `callers` in `edges.yml`, so trilium can reach the new verbs (Still unsure about);
   - switch the operations unit to the binary (the external-unit role change);
   - add `/usr/local/bin/awm-operations` for R85 and the forced command.
5. The loader on deneb. trilium's new `[[needs.tool]] operations` holds only if deneb's loader counts operations as present. The loader accepts a need for a door singleton (`check_needs`, `loader.rs:418`, test `a_need_for_a_door_singleton_holds_with_the_door_down`). But `door_singletons` (`awm-trust` `edges.rs:169`) lists a singleton only when `may_call(node, tool)` holds. Today deneb is not in operations' `callers`, so the loader would refuse trilium. The `edges.yml` change above makes the need hold. Prove it before submitting: run the gateway tree test with deneb's edge list plus the new caller, and check that trilium loads. If the need still fails, plugin-redux/interview fixes the loader in PLAN.md T2.
6. Before submitting T11, tell plugin-redux/interview. It runs that batch as merger and holds it before alcor's rollout. This task then does the manual cutover on alcor:
   1. Run `check-state` on a copy.
   2. Ask Tony to hold Proposed-note edits.
   3. Stop the Python service.
   4. Confirm `writer_mark` equals max(events.id) now, after the stop, so no later event escapes the mirror.
   5. Copy `/var/lib/awm/operations` to `/var/lib/awm/operations.pre-rust-<ts>`.
   6. Tell plugin-redux/interview to resume. The rollout deploys and the first start migrates.
7. Restart trilium on deneb. It imports once and the counts are checked. Then it renders, or on a mismatch it files the `request_human`.
8. Restart list: `awm-plugin@operations` on alcor and trilium on deneb.
9. Rollback:
   1. Stop the Rust unit.
   2. Restore `/var/lib/awm/operations` from the `pre-rust` copy.
   3. Revert the operations and trilium commits on `release`, then deploy.

   The Python service then runs on the restored state. The Python writer resumes the vault mirror from `writer_mark`.

Gotchas:
- A stored passkey that the Rust verifier cannot parse locks Tony out of approvals. `check-state` must pass on a copy first, and R85's `enrol-link` over ssh to alcor is the recovery.
- Canonical JSON must be byte-equal across the browser, Python and Rust, or the device chain locks.
- sign_count follows py_webauthn: both zero passes, otherwise the count must increase.
- Proposed-note edits made between the writer stopping and the trilium renderer starting are lost. Hold them for the window.
- The operations unit's switch to the binary must land in lockstep with the port.
- Long-polls of 55 s need gateway call timeouts above 60 s.
- Today's quirks are kept, not fixed: the Arbutus bundle path is unreachable from the sandboxed unit, and spawn orders to nodes other than alcor are never pulled.
