# Port plan: operations

## Purpose & Contents

This file holds the approved port plan for operations, from task T11 of the ops group's plan (task CWPYAtyb), revised and approved on 2026-11-06. It names only what changes. The Python code and its manifests are the reference for everything the port keeps. The rules that hold for every ops port are in [README.md](README.md). Later changes go in the port commits and the journal.

## Port operations, move the board writer to trilium and migrate the cards

`queue.db` on alcor is already the source of truth. The vault mirrors it one way, except for Tony's Proposed-note bodies and the Ops book notices.

Consolidations:
- **C1.** The writer, the Trilium code, `board/` and `board-install` move to trilium, which stays Python. trilium also owns the rendering of the Ops book (from `ops_list`) and of the Portals note.
- **C3.** The grant code goes to awm-trust.
- **C4.** One binary serves the service, `door --node`, `enrol-link` and `devices`.
- **C5.** ssh certificates are built in process with `ssh-key`.
- **C6.** The static assets are embedded in the binary.

Changes:
1. New hidden verbs for trilium:
   - `board_changes`, the event feed;
   - `card_body_set`, on a Proposed note card, up to 100,000 characters;
   - `board_import`, the one-shot migration, with `accept_gap`.

   Each refuses a call with a session stamp. `card_body_set` and `board_import` also refuse every calling plugin but `trilium`.
2. A new hidden verb, `ops_list`. `ops_post` stores the notice and drops `note_id`.
3. `task_get` gains `body`. The spawn body check compares against `tasks.body`, so a press after an unsynced edit is refused.
4. The Claude Code hook moves to cch. Until cch's port lands, it is the `operations hook` subcommand, with the same marker, notice, 20,000-character cap and exit 0. Whichever lands second deletes it.
5. Caller gate. `devices`, `device_revoke`, `order_pull` and `order_done` admit only operator calls with no plugin caller, plus the plugins whose Python manifest lists that verb today.

`request_human` accepts a service caller, opens a card per call and returns its `task_id`. `task_for_session` and `task_attach` keep their shapes at major 2. The `intake_*` verbs stay unchanged for the untouched board service.

State:
- Every default path and override is kept: `$HOME/.awm/operations/{queue,relay}.db`, which on alcor is `/var/lib/awm/operations/.awm/operations/`, and the approver's database and keys under it.
- A missing `queue.db`, approver key, grant key or ssh CA refuses the start.
- First-start migration, after the snapshot: add `tasks.body`, create `ops_notes`, and set kv `vault_mark` to `writer_mark`.

Card migration:
1. trilium snapshots the vault (`awm-trilium-copy`) before the new renderer first writes.
2. trilium sends one `board_import` with every `#swarmCard` body, archived notes included (`includeArchivedNotes`), and every Ops note.
3. operations checks in one transaction:
   - the card count;
   - the task_id set against the events up to `vault_mark`, resolved through `folded_into`;
   - a body for every note card.
4. On a mismatch, operations writes nothing. trilium files a `request_human` that names the missing and extra ids. Tony fixes the vault or accepts the gap.
5. The vault board stays as it is until the import succeeds, so it is never blank.

Dependencies:
- The secrets and the alcor network are kept.
- No needs. reflection, social, 2fa and zotero are plain calls, and each degrades as today.
- trilium gains `[[needs.tool]] operations` major 2.

External units:
- One cloud-role change moves the operations, social, browser, deneb relay and awm-journal units to the plugin binary and the plugin folder. It drops the miniforge `BindReadOnlyPaths` line. It lands right after T1. Each unit switches when its own port lands.
- The same cloud batch adds deneb to the operations callers in `edges.yml` (Tony: "deneb can join operations callers for now"). The gateway tree test proves that deneb's loader then accepts trilium's need.

Tests:
- Port the Python tests. The approver's tests run against a Rust soft authenticator.
- Test `board_import` for a match, missing ids, extra ids, a replay and `accept_gap`.
- Test the caller-gate refusals.
- Test the migration on a synthetic `queue.db` that Python writes.
- Test the canonical JSON and COSE vectors.
- Before cutover, run `operations check-state` on a copy. It parses every stored credential and key.

Cutover on alcor:
1. Tell plugin-redux/interview, which holds the batch before alcor's rollout.
2. Run `check-state` on a copy, and ask Tony to hold Proposed-note edits.
3. Stop the Python service, and confirm that `writer_mark` equals max(events.id).
4. Run `operations state snapshot`.
5. Tell plugin-redux/interview to resume.
6. Restart trilium on deneb. It imports the cards and checks the counts.
7. Compare the recorded reads by hash. Then run a scratch card's write path. With Tony, run one passkey close and one 2fa refresh.

Rollback:
- Stop the unit, revert both commits, and deploy.
- Keep the live `queue.db`, `approver.db` and `relay.db`. Restoring `approver.db` rewinds the device chain, and restoring `relay.db` replays orders.
- Restore only the store that `state check` proves damaged. For `approver.db`, re-enrol through `enrol-link` instead.
- Restore the vault from trilium's snapshot.
- The Python writer resumes from `writer_mark`.

Gotchas:
- A stored passkey that Rust cannot parse locks Tony out of approvals. `check-state` must pass first, and `enrol-link` over ssh is the recovery.
- Canonical JSON must be byte-equal across the browser, Python and Rust.
- Edits made during the cutover window are lost unless they are held.
- Long-polls of 55 s need gateway timeouts above 60 s.

