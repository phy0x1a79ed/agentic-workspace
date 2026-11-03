# Questions the build lanes paused on

## 1. ANSWERED (a). One harness interface for cch and dsh (redux-core a92d02bf, contract § open)
The harness lane wants one interface, harness/1, that both cch and dsh serve. R117 item 3 refuses two plugins on one tool UUID.
- (a) Add a contract UUID. Each harness tool keeps its own UUID and declares implements = ["<contract-uuid>@1"]. reflection needs the contract, and the contract resolves to every provider. Core leans this way.
- (b) Add no new concept. reflection lists the cch and dsh tool UUIDs as optional needs. A third harness means editing each caller.
Interim: the loader accepts both forms. The lanes build to (b) for now, per "Keep the design simple. Add no feature until a path needs it" (2026-11-01). Switching to (a) is a small change.

## 2. ANSWERED (a), "Fold virtrmic". mic needs virtmic (redux-plugins b4dbaa5a, task T2, R133 vs R134)
virtmic owns the PulseAudio daemon and the `virtmic` null-sink. mic's bridge calls `virtmic ensure` before every stream. Dropping virtmic as is leaves mic streaming into nothing.
- (a) Fold virtmic's sink upkeep, about 300 lines, into the mic plugin. Drop the virtmic service and tool. The lane prefers this.
- (b) Keep virtmic as its own plugin.
- (c) Drop it and leave mic broken.
Interim: the lane builds (a). It keeps mic working, as R133 needs ("Mic is needed to get audio into remote boxes"), and removes virtmic as a service, as R134 says. It sits on a local branch and is easy to undo.

## 3. ANSWERED (a), "Waiting sessions  hold scope". Does a session in `reflection wait` keep its scope claim? (redux-sessions 2ec1b21d, task T3, R3, R42, R51)
- (a) Yes, while harness's wait ledger lists the session, so a waiting coordinator keeps its feature scope. The lane prefers this.
- (b) No. The claim dies with the process, and the session claims the scope again when it wakes.
Interim: the lane builds (a), with the claim rule in one place.

## 4. ANSWERED: a passkey on every request, and one request may carry a batch or open a window. Passkey on every queue action, or only at login and on risk? (redux-queue 593a3cae, R64, R65)
R64 says "Every login and every action takes the passkey". R65 says the panel needs no action beyond the button. Tony: "i dont want to have open on every prompt".
- (A) The passkey opens a panel session of 12 h and fires again on a risk signal (new device, new network, high rate). Between those, the device key signs each action on its hash chain with no prompt. The lane prefers this.
- (B) A passkey assertion (Face ID or Touch ID) on every action, with its challenge bound to the chain head.
Interim: the lane builds (A). The setting `passkey_each_action` switches to (B).

## Coordinator calls, for review
- redux-channels T9 (R77): delete the board door and intake in the same change that makes rooms carry federation threads, not earlier. R77 already settles that they go, so only the timing was open.
- redux-channels made a local project `tether`, a clone of the GitHub tether repo, for R138 and the relay work. Local commits only.

## 5. ANSWERED: awm is the product and the cloud project holds the fleet (fleet.md R140). Retire the cloud project's deneb scripts? (redux-fleet 061af0f6)
deneb's config moves into awm's `fleet/` tree. The cloud project's VMs/tencent/deneb scripts stay untouched for now. Retire them once the config pass lands?

## Coordinator calls, continued
- redux-fleet T11 (R83): the Zotero Sync now button waits for the R65 panel, because R64 says alcor authorizes every action. The lane builds it against the queue lane's panel interface. Only the timer-off config ships alone.

## 6. Scope delete guards (redux-sessions 2ec1b21d, task T4, scopes.md R44)
6a. R44 item 4, unpushed commits block a delete. `scope delete` keeps the branch by default, so the commits survive.
- (a) Block only when the delete also removes the branch (delete_branch=true). This is built now.
- (b) Block every delete of a scope with unpushed commits, even when the branch stays.
6b. R44 item 3, ignored files outside .awm/ block a delete.
- (a) Exempt DVC outputs pinned by a tracked .dvc file, plus regenerable caches (__pycache__, .pytest_cache, .mypy_cache, .ruff_cache, node_modules, *.pyc, .dvc/tmp, .dvc/cache). This is built now. Without the exemptions, every scope that ran tests needs force, so force turns routine.
- (b) No exemptions except .awm/.
Both flips are small.

## 7. Drop the retired awm_penpot table? (redux-plugins b4dbaa5a, task T2, R134)
An upgraded auth DB still holds the retired `awm_penpot` table. It stores Penpot passwords in plaintext, and nothing reads it now.
- (a) Add an auth migration that drops the table at the next deploy. It deletes only dead credentials, but it cannot be undone. The lane prefers this.
- (b) Leave the table, and delete it by hand in the sweep. This is taken for now.

## Notes for the sweep
- The install-awm.sh guard on deneb (the cloud project's script, whose comments still say sirius) needs AWM_ALLOW_DISABLE=1 once penpot-view and penpot-plugins leave PUBLIC_SERVICES.
- The base commit already fails 9 social Slack tests and 1 dvc test. The lanes did not cause those failures.
- The Rust gateway needs feat/redux-core's gatewayclient, which sends a per-spawn service token on register. An older adapter gets 403, so every plugin's adapter moves to it in the same sweep.
- feat/redux-trust and feat/redux-core merge cleanly. Every test passed on a scratch merge.
- Nothing runs the Rust gateway live yet.

## 8. Long-lived federation links when the board door goes (redux-channels 5a3ff6e5, task T9, channels.md R77)
Rooms now work end to end at awm 403d003e: relay rooms, the channel tool, encrypt and the machine kind. A room is short-lived and one task holds it: in memory, 24 h at most, closed when its holder's job ends (R72). Caviar's stream listener and the muse two-way link both run on the federation board today, and both are long-lived. Moving them onto rooms needs a change on their side, plus one of these:
- (a) A standing task on our side holds a room for each link and reopens it when it expires. That is the R72 rule with a task that never ends.
- (b) Each conversation rings a seat doorbell (R77), and the seat opens a fresh room. The doorbell is not built yet.
- (c) A persistent room kind that outlives a task, as an exception to R72.
Until you answer, the board door and intake stay in place. Their deletion waits on this.
- feat/redux-sessions merged origin/dev, so it carries dev commits that the other lanes lack. Expect a social merge between feat/redux-sessions and feat/redux-trust 08660b2e.

## 9. The room link path (redux-channels, channels.md R72 item 1)
R72 shows `https://nexus.tony-xy-liu.com/r/otter-lamp-quarry`. The edge serves the relay under `/tether`, so today a room link is `.../tether/r/otter-lamp-quarry`.
- (a) Keep `/tether/r/<name>`, and fix R72's example.
- (b) Add an `/r/` prefix on the edge, for shorter links.
The relay serves `/r/<name>` either way.

## Sweep merge notes from redux-build/harness, carried for redux-build/sessions, which has stopped
1. The cx edits on feat/redux-sessions now live in cch. Files moved from awm/services/cx/awm/cx/<mod>.py to awm/services/cch/awm/cch/<mod>.py: lifecycle, sessions, claim, pool, seed, config, trust, reconcile and remove. cx's hub_adapter is now cch/cx_adapter.py, and keep's is cch/keep_adapter.py. Git sees the merge of feat/redux-sessions with feat/redux-harness as delete-vs-modify, so re-apply those edits by hand on the new paths.
2. `reflection wait {until:[R9 ids]}` and the hidden `waits {}` verb are built (ced70632). A waits row holds session_id, job, until, since_ms, project and scope. The precheck in the sessions lane's retire draft (`awm reflection wait --help`) should now pass, which unblocks the parallel-work skill install.
3. `reflection stop` keeps the job by default. `remove: true` deletes it. The retire skill never passes remove.
4. whoami calls reporting's `task_for_session {session: <R9 id>}`.
5. Three uses stay on awm.claudedaemon: the gateway's sessionmode lookup, reporting's job-state reads and transcripts' live-id check. The reasons are in redux-harness's plan § Callouts. The gateway one belongs to core. For that change, bring core back with resume_other a92d02bf.

## Sweep notes from redux-build/plugins
- Merge feat/redux-plugins and feat/redux-core in one sweep. ARCHITECTURE.md and README.md link to core's plugin-contract.md.
- Your global ~/.claude/CLAUDE.md (tier 2) and the deployed workspace AGENTS.md still send agents to graphify. Edit them in the sweep that deploys the removal, not before.
- The compute Claude Code hook stays on mizar. It fails open. redux-build/fleet has the cleanup.
- awm/package.json still lists the notes page's editor dependencies. Prune them with npm in the sweep.
- kb's "library" archive table on search-finish's feat/svc-kb moves into kb's awm-plugin.toml at merge. search-finish agreed to do it.
- Seven legacy folders are not yet on the plugin contract: auth, board, reporting, cx, keep, reflection and search.

## Sweep notes from redux-build/trust (docs/redux/drafts/redux-trust-plan.md § Callouts)
- These land together or a lane breaks:
  - The gateway passes `_caller_trust` and `_caller_scope` beside `_caller_session`, reading reflection's trust.json. A session with no entry counts as trusted. Core was asked to build this tonight.
  - The social keeper passes `trust` to `cx start`.
  - The fleet config pass applies cloud/redux-trust's roles and writes one `[[external]]` line per singleton.
  - `rlm-browser` retires when the browser singleton runs, because both publish `browser`.
- R91 (shogun), R94 (lyra) and R96 (the Tailscale moves) need you at the keyboard. They are on the batch list in docs/redux/drafts/edges.md.
- altair's auth migration 5 to 6 drops its static accounts and revokes every unscoped device token. A device that used one needs a new scoped token.
- The journal database moves to /var/lib/awm/journal when the journal runs as its own user.
- A per-seat browser container needs rootless Docker for the browser's system user.
- resume_other refusal (a92d02bf, c63d29b9): fixed on feat/redux-harness b6394431, with a test. On the live code, retire runs `reflection stop`, which runs `claude rm`. cch's prune_lineage then drops the start_other record about two minutes later. On the branch, stop keeps the job by default. A job already retired on the live code comes back with `claude --resume <uuid>` in its worktree.
- T16 isolated container sessions are built on feat/redux-harness. They need a gateway door, which harness sent to redux-build/core-caller.
- reflection's and operations' own-session verbs must read the new _caller_session_id. Today they key on _caller_pid (redux-core-plan.md § Callouts).

## 10. Fleet values only you can give (redux-fleet, plan § Callouts)
- Confirm the approver's origin name: approve.tony-xy-liu.com?
- Fill approver_grant_pub, awm_ssh_user_ca, capella's wsl_windows_user and lyra's addresses.
- showtime is missing from edges.yml, and engcyc runs there. sirius is missing too, but deneb replaced it on 2026-09-15, so it probably stays out.
- Check that /trilium/etapi on nexus reaches Trilium.
