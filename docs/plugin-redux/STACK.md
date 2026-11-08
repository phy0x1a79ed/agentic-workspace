# Plugin redux — interview stack

Goal: settle what the awm plugin redux covers and how plugins install and run now that the core is Rust, then submit a formal plan. Prompt (2026-11-06): "we moved to rust... why are we even doing pip installs?"

Decisions: `search` for journals tagged `decision` in awm/plugin-redux.

## Current

- `build` — T6 (land as merger). Landed: T1 49232bf9, T2 38bcdafe, knowledge T3 fd430e0d (pycompat, store, preserve_order, protoc), knowledge T4 a1a2ac46 (marks). Held: mic and transcripts (wait for ops' awm-statecheck; transcripts also waits on Tony's review of the sweep counts). knowledge is replanning the one-LanceDB-in-journal cut with Tony (decision c2419412).
- `ops-land-1` — port-ops asks OK to land feat/port-ops 67a90fb7 in one submit, --restart fileviewer,dashboard,routine (brief /home/tony/.claude/jobs/8efddb16/tmp/land-brief-ops1.md). Carries awm-trust, awm-statecheck, awm-dvcdata, fileviewer, dashboard, routine with events folded in (Rust `routine land` replaces land.py; this batch still runs land.py), and a land.sh workspace fix. State check on a copy of mizar's state: lost []. My review: sound; statecheck lands with routine, so the state rule holds. Gaps: inventory covers only alcor, altair, capella, mizar; no manual rollback if the Rust queue breaks after the land. Asked Tony (his hold covers replies to workers).
- `comms-land` — port-comms at d0c481e2 (merged release a1a2ac46, 37 suites green) holds T12 for my go. Goes after ops-land-1, since comms has state and needs statecheck.
- `edge-links` — after the reboot mizar reaches only alcor and capella over the edge. altair's sshd has ExposeAuthInfo no (arbutus.yml keeps altair's sshd outside our roles, shogun manages it), and edge-serve refuses without SSH_USER_AUTH. shogun has no ExposeAuthInfo line either. deneb has it, but its link still drops. Not caused by the move (altair's link dropped back on 11-06). For Tony.
- `home-move` — DONE 2026-11-07: /home is on sda1 (916 G), mizar rebooted, 21 enabled services ready, queue empty. Workers respawned with `claude respawn <id>` one at a time (resume_other refuses after a reboot; never --all, seat:muse stays down) and told to resume. /home.pre-move keeps the old copy until Tony OKs its deletion. Park record: Park points: comms 39ea5c7f; knowledge a2bbfe68 (+ feat/port-knowledge-mic 9282fa47); harness 14231aa6 (WIP partial hooks.rs); trilium 1101fb45 (+ trilium-telemetry and trilium-vault sessions at cf30928e, parked by trilium); ops 27e1fb34 (fileviewer 66cca782, dashboard 2cdd6cb5, routine+events 27e1fb34, workspace fix 9e4feab2; lands held for my OK, sends briefs on resume). knowledge: Tony redesigned the group in its session (one plugin renamed kb serves journal and search, one LanceDB per source: journals, guide, papers+Zotero, transcripts; transcripts folds in); it asked Tony three follow-ups (nodes and user, papers convert vs re-embed, transcripts regex) and then sends the new order. Handoff ~/.claude/plans/handoff-T11.md. Earlier state: mizar's /home moves onto mira's 1 TB disk (plan /home/tony/.claude/plans/quizzical-munching-stroustrup.md, approved by Tony). mizar-disk/interview (job 8999ba9a) formats the disk and runs the bulk copy while the workers keep working. The coordinator is mizar-disk/cutover (job 0eac795e on alcor, scope cloud/mizar-cutover; mizar-disk 8999ba9a has retired). It asks me to park the five workers (ops, comms, harness, knowledge, trilium): each commits, pushes to github, stops its builds and subagents, and replies "parked". When all five have parked and `land.sh status` shows lease and batch null, send the coordinator "workers parked" with reflection(verb="message", peer="alcor", args={job:"0eac795e", text:"workers parked"}). After the reboot it resumes me with a followup; I then resume each worker with resume_other. Tony's OK to message the workers, relayed by mizar-disk, covers the park and the resume only.

## Waiting on the user

- engine: DECIDED by Tony 2026-11-07, after the bench (journal 6939906b, libSQL failed). search becomes a federator with no content store; each source owns its engine through a new awm-index crate (LanceDB, event-driven maintenance, yearly sections); journal's posts move into its own Lance tables, with relational state staying in journal.db; scopes drops its vector table. "I really dont want multiple copies", "I really hate timers ... make this event based". knowledge is rewriting the plan. Check it for: posts tables kept read-only plus export-back rollback, cross-store write order plus reconcile, peer-sync dedup, per-source privacy, the section trigger, the down-source errors[], and the describe changes.

- Pre-existing: the transcripts archive is 0664 in 0775 dirs (the live transcripts are 0600). knowledge's port writes it 0600/0700. Raise with Tony at the knowledge approval.

- sites daemon: does "mira is going away" cover alcor's awm-sites (awm/services/social/mira/, port 7822; serves Slack, Teams and onedrive-ubc)? request_human filed. comms keeps it running until the answer.

- isolated/ tag: dvc's nightly Globus mirror copies .awm/ off-site, skipping only CACHEDIR.TAG folders. statecheck now tags every .pre-rust/ and .rust-<ts>/. Pre-existing: .awm/services/cch/isolated/<uuid>/claude/.credentials.json is mirrored too. Asked Tony whether to tag isolated/.
- lyra: cloud roles applied 2026-11-07 (cloud dev 8b774a6 on mizar's bare: host_vars plus lyra's service key; altair's origin rejects the push while a worktree holds dev). lyra is asleep now. When it wakes: run journal_move.yml -l lyra, then awm reload, then site.yml --tags service_key on the doors so they admit lyra's key; re-run lyra's roles in check mode clean before cloud dev goes to release.
- mizar's awm-edge-fwd@deneb was hand-installed (no repo writes it) and looped since ssh_edges turned on ControlMaster. Fixed in place with ControlMaster=no and ControlPath=none. Follow-up: move the unit into a cloud role.
- consolidate: CLOSED 2026-11-07 (decision journal). Option A stays: a binary and a process per plugin. guide moves into journal, which holds the one LanceDB. search is a pure federator. kb's shape is open: knowledge asks Tony. Tony OK'd directing knowledge (sent: T2 in, submit T3, T4 and mic in order; hold awm-index, guide and search lands).
- port-trilium (job c6adc424, awm/trilium-design, feat/trilium-design) joined at Tony's request: the new core plugin telemetry, plus the trilium vault side. Answered: it does C1 (the board writer move) and port-ops drops C1, pending Tony's OK to tell ops. It lands its own nested scopes through land.sh. CONTRACT.md items come to me, one per port. The awm/plugins topic already covers its "plugin up/down".
- mizar disk: mira's 1 TB SSD is sda, unmounted. mizar-disk/interview (job 8999ba9a, cloud/mizar-disk) interviews Tony and preps it. Tony: mount it if needed, but it will likely be formatted. Speeds: sda reads 460 MB/s and 74k IOPS, sdb 452 MB/s and 70k IOPS; sdb writes 508 MB/s and 325 fsync IOPS. Freed 2026-11-07: docker build cache 19 GB and my idle gw/sdk targets 20.5 GB.
- Waiting on Tony: may I tell port-ops to drop C1 (port-trilium does the board-writer move)? May I hand port-trilium's CONTRACT.md items to ops, comms and harness? His hold on messages to the workers still stands outside the park and resume.
- Worker notes held for Tony: harness's reflection waits on ops' awm-trust; harness ports all of `kept` into awm-harness (comms' plan gave it to comms); dsh's three decisions for comms; knowledge's lancedb binaries are ~238 MB each (budget?); ~/.claude.json is 664 on capella and altair.

- (superseded by `lyra edges` above) lyra: land batch 9a59ed8ad3de (ticket b3b597e326e0) is stuck at lyra, which has no edges.yml and was never provisioned. Choose (a) provision it with cloud awm_config and edges, or (b) shut lyra's WSL so the rollout skips it as an asleep sleeper. request_human is filed and the lease is held. deneb, shogun and altair still run the old release.
- Plan questions for Tony at each approval, in the plan's "Still unsure about": comms D2 (the mira daemon) and D3 (rlm-browser); harness Q1 (the dsh fork build); knowledge (search off LanceDB, the model artifact, kb's Python child and kb's tool vs R134, the journal embeddings tables, transcripts' sweep); ops (deneb as an operations caller in edges.yml).

## Preferences

- 2026-11-06: four workers, balanced by size, not one per plugin. Core plugins may share crates.
- 2026-11-06: planners are Opus high, and they execute after planning. This session merges.
- 2026-11-06: run the walk with dfs-interview. "when we have things scoped out, formally submit a plan."
- 2026-11-07: planners run in plan mode. Correct a running worker by message, never by restart (memory planners-plan-mode-nudge).
- 2026-11-07: T4 revised. Each worker sends its plan here, this session checks it alone and says "submit", the worker calls ExitPlanMode, and Tony approves each plan individually. No merge gate across workers.
- 2026-11-07: T3 waited for seats-pause's message before starting.

## Stack

- `build` — the approved plan, T1–T10. The todo list mirrors it.
- `t2-finish` — DONE at 95a2f71c (all ten items plus seven audit fixes; Rust 145 pass, Python run-tests.sh green; workers told via ~/.cache/plugin-redux-briefs/t2-done.txt). Waiting to land: T1 batch 9a59ed8ad3de is held at lyra (no edges.yml, gateway inactive; Tony fixing edges). Open: an unauthenticated loopback POST reads as trusted with no plugin, so it passes auth's caller gate (existing open-loopback caveat). Earlier notes: T2 commits on feat/plugin-redux: pins 891e8692, gateway 0e423392, SDK 29900ddb. Before T2 lands:
  - delete test/contract/test_plugin_calls.py and awm/services/search/tests/test_needs.py (they test the removed guard), and fix the guard mentions in the INSTALL.md of rlm-factorio and search;
  - make a [[needs.contract]] with no provider refuse (R117 item 8), and fix contract § Contracts item 6 and its CAUTION;
  - contract doc: the core roots include core/plugins; the install stamp skips UNSTAMPED; the plugin state dir is <AWM_DIR>/plugins/<plugin>/;
  - the core version covers awm-cli's dependency closure, not core/ minus plugins;
  - add plugins/* to core/Cargo.toml members, and --locked to release-core.sh;
  - page pipeline: core/plugins in awm/scripts/build.sh, scripts/release-pages.sh and awm/tsconfig.pages.json; pins.sh pages_version hashes core/plugins/*/pages/*;
  - root plugins' [[service.archive]] readable whether loaded or not (search kinds());
  - the gateway's call frame carries `plugin` (SDK Call.plugin);
  - the SDK's privacy module re-exports awm_trust::privacy once ops moves it there;
  - awm-trust's door far end answers `not installed`, not "Unknown tool".
  - disk regrowth: each worktree's core/target holds 4-5 GB. On 2026-11-07 mizar filled to 100%, and deleting 26 idle scopes' targets plus the docker build cache freed 82 GB. Fix: rust-build.sh defaults CARGO_TARGET_DIR to ~/.cache/awm-target/<worktree>, and a gc removes the targets of worktrees with no live session.
  - Wire awm_statecheck::snapshot_once into Service::run after ops lands the crate (shape: ~/.cache/plugin-redux-briefs/statecheck-shape.md; size_only/extra accept outside paths and write nothing there; CACHEDIR.TAG in every .pre-rust).
  - SDK asks from ops: a /hub/plugins accessor with a change notice (auth fronts), operator_dm (routine), a page usage event (operations). After ops lands awm-statecheck, Service::run calls awm_statecheck::snapshot_once before on_start.
  - The T2 state audit (at c7182d93) found it safe after fixes, all sent to the finish subagent: (1, blocking) remove `install =` from the nine Python manifests, since the gateway would run those hooks with no conda and refuse the plugins; (2, blocking) the loader ignores and warns on a need's `verbs`/`optional` (stock's rt on alcor still has verbs); (3) awm-deploy forces the restarts it names, so a kept breaker can't hold a plugin down; (4) the contract says plugins/<plugin>/ holds only the stamp, and state stays in services/<service>/; (5) a CAUTION: a rollback moves the tree and the binary together. deneb's /var/lib/awm was unreadable to the audit.
  The isolated-door e2e test flaked once (it listed only describe).
- `cross-group` — settled so far:
  - social calls only reflection. reflection gains probe, the `gone` code, transcript paths, include_stopped and typed start codes. list_other answers ok:true with errors[].
  - Transcripts are read on the seat's node.
  - transcript_tail goes into awm-harness for cch only.
  - preserve_order is on for the whole workspace (harness's awm-harness land restarts every Rust plugin once).
  - R9 ids move to awm-proto.
  - privacy moves to awm_trust::privacy.
  - One generic cloud-role drop-in for external units (alcor operations/social/browser, deneb relay, awm-journal on mizar, alcor and capella) lands right after awm-trust.
  - awm-store lands first, with ops' extras.
  - The operations hook stays until cch replaces it; whichever lands second deletes it.
  - dsh's front is [[service.front]]: it reuses the edge leaf and is landed by harness.
  - claude-science declares [[service.front]].
  - The T11 operations cutover holds the batch before alcor.
  - R135 item 6 needs amending: social calls reflection only.
- `seat-redesign` — seats/pause (now retiring) handed social's seat redesign to the comms port. Settled: S1–S5 answered; seat folders live at projects/agentic-commons/seats/<slug>, each its own registered scope; start with a plain start_other on that scope (no cwd/claim params); briefs are passed as absolute paths into main/docs/. New seat: `scope create` with peer runs a project-declared create hook (agentic-commons: scripts/new-seat.sh). knowledge adds the hook support in scopes and the declaration in agentic-commons. Cutover list: awm feat/seat-pause docs/seat-lifecycle/handoff/README.md. Open with Tony: tag isolated/ (pre-existing login copies mirrored off-site).
- `state-safety` — Tony 2026-11-07: "they must not leak pre-redux state". Common rule in ~/.cache/plugin-redux-briefs/state-safety.txt. ops builds core/crates/awm-statecheck (inventory, snapshot, check, restore; .pre-rust/ via the SQLite backup API), the one shared crate for all groups. Audits back: T2 (fixes 1-6 sent to the finish subagent; fix 6 is the caller gate on auth/operations secrets), ops (11 fixes sent), comms (13 fixes sent). harness (11 fixes sent), knowledge (11 fixes sent). All four plans are audited. comms and harness were told to submit after two edits each. The rule for all four: roll back by reverting the code and keeping the live state; restore .pre-rust only on proven damage; deleting .pre-rust is a listed step with Tony's OK. ops was also asked for an atomic .pre-rust.tmp rename, a snapshot CLI, a disk check and a damage-only restore flag. Five read-only audits started as subagents: the four plans plus the T2 diff. Their results arrive as task notifications. Every plan's submit is held until its audit is back. comms already submitted once before the audit; tell Tony if its audit finds anything. On each result: send the fixes to the worker, then "submit", and the worker must re-send any later change to its plan for a new "submit". The T2 finish subagent (items in `t2-finish`) is also running. When it reports: push feat/plugin-redux, tell all four workers to merge it, submit T2 through land.sh (after T1's batch clears), and tell workers when the SDK has landed.
- `reflect-sender` — Tony 2026-11-07: "reflect needs to prefix with sender like how socials seats work. otherwise agetns dont know if its me or other agents talking to them". reflection message delivers the bare text into the target's prompt, so it reads as Tony's own input (the cutover coordinator's park request arrived this way). Social's seat line is `<platform> [<level>] from <sender> [channel=<id> message_id=<id>]: <text>` (social/seats.py:79). Proposed: reflection prefixes `awm from <session name> [job=<id> node=<node>]: <text>`, set by the gateway from the caller's frame, not by the sender. Owner: port-harness (reflection port). Asked Tony.
- `live-bugs` — from the knowledge plan:
  - the scatter-gather skill calls gather/scatter, but the verbs are git_gather/git_scatter;
  - goal-alignment calls removed journal verbs;
  - debrief passes kind:"journal", which the gateway refuses;
  - mizar and alcor hold an agent-comms tag under different ids. Rename one before journal lands, or the first sync blocks journal reads between them.
- `hot-reload-followups` — --force and adopted env are done in 0e423392, with tests. Open: deneb relay linger; hold-mode blocked card unverified.
- `refresh-handoff` — deterministic-refresh landed in 5bbbd3ae. lyra (2026-11-07): `awm` remote set to the GitHub backup (lyra's key is not on mizar), socat installed, and a hand-written config.toml (backup config.toml.bak-20261107). Still open: mizar-push, and the stock deploy.toml `[refresh] kind="none"` fix.

## Workers

- engine-bench — job 5367f4a7, plugin-redux/engine-bench, scope awm/engine-bench, Opus high, https://claude.ai/code/session_012TZ1zx2Aqin2tp18ZYJSQo. Brief ~/.cache/plugin-redux-briefs/engine-bench.md (+ engine-bench-spec.md from knowledge's T6). Started 2026-11-07 at Tony's ask. Reported 2026-11-07; retiring.

The worker branches build on feat/plugin-redux (merged at 6ee5a267 on 2026-11-07, after Tony noticed they branched from release). CAUTION: this worktree's `origin` is altair's bare, not mizar's. All scopes share mizar's bare, so tell workers to merge the local branch `feat/plugin-redux`, not origin/. Each time feat/plugin-redux moves, push it to github and tell the four workers to merge it again, until T2 lands.

Briefs: ~/.cache/plugin-redux-briefs/full-<group>.md. All Opus high, task CWPYAtyb, in plan mode.
- comms — job e44d901a, plugin-redux/port-comms, https://claude.ai/code/session_012TZ1zwTFfh7Txjdm2h5Rma. Plan /home/tony/.claude/plans/kind-stargazing-falcon.md: APPROVED by Tony. Paused at Tony's ask after compacting; T0 (merge feat/plugin-redux) waits for his go.
- harness — job eecc2448, plugin-redux/port-harness, https://claude.ai/code/session_012TZ1zx4PnARsz9Am5dpp85. Plan /home/tony/.claude/plans/sparkling-sniffing-sonnet.md: APPROVED by Tony. Paused at Tony's ask after compacting; T0 waits for his go. Submits no land until T2 lands.
- knowledge — job 2a70a96d, plugin-redux/port-knowledge, https://claude.ai/code/session_012TZ1zwibR5V8A9BD4Hq2tt. Plan /home/tony/.claude/plans/hidden-rolling-bentley.md: APPROVED by Tony (federated search, guide, awm-index). Executing from T1. Holds every land until the T2 SDK and awm-statecheck are in, and guide's land until the awm/plugins event sha (gateway subagent building it on feat/plugin-redux).
- (all four plans now carry: the T0 merge of feat/plugin-redux, state-safety.txt, awm-statecheck state.toml files; every revision returns here for "submit")
- ops — job 8efddb16, plugin-redux/port-ops, https://claude.ai/code/session_012TZ1zxbfDVLySsTQCPp1rq. Plan /home/tony/.claude/plans/abundant-singing-moon.md: APPROVED by Tony, revision too. Merged feat/plugin-redux at 136c1022. Paused at Tony's ask after compacting; then port-docs refresh, T1 awm-trust, T14 awm-statecheck.

## Closed

- `core-set` — journal 2a343d6c (core set, consolidations, wake not core, kb its own source plugin, reserved names).
- `deps-semantics` — journal a5f837ac: a need is a must; calls need no declaration; no optional flag.
- `board-store` — journal fc14982b: operations tracks the queue state; trilium exposes and manages it.
- `plan` — journal 5d8bd9db: four Opus-high workers by concern (~17k lines each), shared crates allowed in core; PLAN.md.
- `rust-port` — journal 1c0f4acc: core plugins port to Rust; any language for others; consolidate; per-plugin planners.
