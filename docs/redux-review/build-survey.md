# awm redux build survey and status (2026-11-03)

Read-only survey. Spec: awm `feat/redux` 0fef1ad4, docs-only (21 doc files on top of b664e80f). `origin/release` = 266f4b29; local `release` ref is stale at b664e80f. feat/redux is 318 ahead of and 10 behind origin/release (behind = R141 fix 5453644f, keeper witness bb028145, journal inbox a086a588, claudedaemon PTY afb46fbb, social INSTALL).

This file is the build's status table. The coordinator updates it at each gather. Rows in § 3 name the lane that built each item; § 5 records where each lane landed.

Method notes. Conflict checks use the read-only 3-way `git merge-tree <base> <a> <b>` (`--write-tree` would write objects, which plan mode forbids). The checks are pairwise, not against a real accumulated tree. Under "Requirement status" the lane reports are the source, with spot greps where marked.

## 1. Branch graph

The redux base points are e10a8175 ("fix quoting in R107 item 7", after the morning review) and 6d83b353 (before it, older spec). "ahead" counts non-merge commits missing from both feat/redux and origin/release.

| Lane | Tip | mb feat/redux | mb origin/release | Non-merge ahead | Contains (whole) | Contains (part) |
|---|---|---|---|---|---|---|
| core | 4ef3593b | e10a8175 | b664e80f (10 behind) | 22 | none | none |
| plugins | fa94487f | e10a8175 | b664e80f (10 behind) | 38 | core | none |
| harness | 9e729b01 | e10a8175 | 266f4b29 (current) | 68 | sessions | core 22/23 (missing 4ef3593b), plugins 44/46 (missing 4ef3593b and the fa94487f merge) |
| sessions | 9ca2ce61 | 6d83b353 | 848f87e5 (2 behind) | 12 | none | none |
| journal | 31f07e27 | e10a8175 | 266f4b29 (current) | 73 (5 own) | harness, sessions | core 22/23, plugins 44/46 |
| queue | f6edcba3 | e10a8175 | b664e80f (10 behind) | 8 | none | none |
| channels | 24c4bd6b | e10a8175 | b664e80f (10 behind) | 11 | none | none |
| fleet | 2281c0a0 | 6d83b353 | b664e80f (10 behind) | 10 | none | none |
| routines | 1df74bfc | 6d83b353 | b664e80f (10 behind) | 5 | none | none |
| trust | 5ae958f1 | 6d83b353 | b664e80f (10 behind) | 25 (17 own) | none | core 8/23 (up to 759e516a, contract v1.4) |

- journal is the superset lane. It holds harness, sessions, origin/release and all of core and plugins except core 4ef3593b (gateway asks `reflection session_of`).
- Lanes in other repos:
  - tether `feat/redux-channels` at 94a30f2. Unpushed. It holds R76 and R138, and awm pins tether v0.2.0.
  - cloud `feat/redux-fleet` at 9eba89b. It holds cloud `feat/redux-trust` a547baa. Both are unmerged.
  - kb `feat/shards` and awm `feat/svc-kb` live on altair and were not checked.
- Four lanes forked at 6d83b353 and missed every spec commit from 1857fa2e on: sessions, fleet, routines and trust. These include R140, R44 item 3, R10/R51, R117 names, R145, R72 and R107 items 5 to 10. harness and journal re-merged feat/redux at e10a8175. Every lane missed the commits after e10a8175, listed in § 3 as stale.

## 2. Conflict check

Proposed merge order onto feat/redux (or dev):

1. journal
2. plugins, which brings core's tip
3. queue
4. fleet
5. routines
6. channels
7. trust

All lanes merge cleanly with feat/redux (docs only). All but trust merge cleanly with origin/release.

| Step | Conflicts vs earlier steps (C = content, MD = modify/delete) |
|---|---|
| 1 journal | none |
| 2 plugins | none vs journal |
| 3 queue | none vs any lane |
| 4 fleet | none vs any lane |
| 5 routines | C `awm/gateway/scripts/run-tests.sh` (vs plugins and journal) |
| 6 channels | C `awm/gateway/awm/gateway/server.py`, `awm/gateway/scripts/run-tests.sh` (also vs routines), `awm/gateway/tests/test_cli_service_commands.py`. MD `awm/services/tether/install.sh`, `tether/awm/tether/callers.py`, `tether/tests/test_owners.py` (channels deletes tether, harness/plugins edited it) |
| 7 trust | C `gateway/aliases.py`, `gateway/tests/test_gateway_ops.py`, `auth/{hub_adapter,service,store}.py`, `auth/tests/test_password_push.py`, `httpsfront/{policy,proxy}.py`, `httpsfront/tests/test_peer_identity.py`, `journal/{channel,dao}.py`, `journal/operations/posts.py`, `journal/install.sh`, `journal/tests/test_federation.py`, `kb/hub_adapter.py`, `search/hub_adapter.py`, `social/{hub_adapter,keeper}.py`, `core/crates/awm-cli/src/lib.rs`. MD `auth/tests/test_users.py`, `httpsfront/tests/test_penpot_route.py`, `kb/tests/test_foreign_recall.py`, `cx/{hub_adapter,lifecycle}.py`, `cx/tests/{conftest,test_lifecycle,test_sessions}.py`, `journal/operations/goals.py`, `reflection/tests/test_reflection_adapter.py` (29 files). vs origin/release alone: journal dao/posts/test_federation, social hub_adapter/keeper |

Semantic seams that merge-tree does not see. The lane reports name them:
- trust's journal migration redefines (1,2). The order must be cursors (1,2), reads (2,3), versions (3,4, placeholder at `dao.py:155`), then tags (4,5).
- trust's addenda fold writes `kind='folded'`, and no `kind` column exists after journal. It needs a tag.
- `peer_gate()` and `tagsync.peers()` must move to the trust edge list.
- `Caller.scope` must be added to 4 struct literals in trust's crates.
- trust's `awm-edge` implements the v1.4 `PeerTransport`. Current core reads `GET /hub/table` and must post `table_changed` over the edge.
- Manifests still in the UUID form, against contract v1.8 names: `awm/plugins/routine`, `awm/plugins/wake` and `awm/plugins/browser`. operations and channel already use names.
- Every adapter needs core's gatewayclient with the per-spawn token, or the Rust gateway answers 403.
- The own-session verbs of reflection and operations must read `_caller_session_id`.
- `rlm-browser` and trust's browser singleton both publish the tool `browser`.
- Task ids are minted three times: operations, `reporting/task_ids.py` and `queue-relay/ids.py`. The grant vectors are copied twice (queue and trust).
- The R69 Pusher is duplicated in `channel/holders.py:159` and `social/seats.py:125`.
- The routines smoke budget needs core's `silent_core` fixture fix, which arrives with step 2.

## 3. Requirement status

Key:
- **B**: built.
- **P**: partial.
- **S**: stale (built against an older text, or not touched since the text changed).
- **M**: missing.
- **N/A**: a rule or a given, with no code to build.

"not applied" means the config exists but has not run on a host.

### install.md
| R | Status | Where / what is missing |
|---|---|---|
| R112 | B | rust 5ab900eb deletes the Python gateway; the Rust core serves every route (80518c29, e3265058). rust a4494351 `awm deploy` places the binary, unit and config.toml per node, untested until the first node deploy. Not applied. MCP `tools/list_changed` not ported |
| R113 | B | core ae8306d5, 8c678557 |
| R114 | P | Each deploy pushes `release` to the GitHub backup, and a batch fails when the backup has diverged (deploy 366d4d48, e43f70aa). Left for T27: origin is still altair, and mizar has no `github` remote yet (`scripts/move-canonical.sh`) |
| R115 | P | Folder install with manifests and pins built (plugins 503291d8, 14639f7b; core 341dd669 `installed.toml`); rust 3d480f75 drops the legacy loader. Git install at a pinned commit, with approval (gateway 3aea4d3d). item 1 B: pages live in their plugins at `/p/<plugin>/<page>/` (rust 82140b59). item 4 trilium prepared: `server.pin` and install mode `pin` with sha256 (cutover 0b22c138); the pin is empty until the fork moves to mizar and releases, tarball host open with Tony. Missing: optional plugins as their own projects (cloud nodes c8d52be `plugin_homes` runs at T27, the awm folders go at T23) |
| R116.1 | P | secrets (core 341dd669). network through a per-plugin egress proxy (gateway 96330d54); every service is held to its `needs.network` and services run on the Rust gateway (rust 3d480f75, 5ab900eb). Clients that ignore the proxy are not checked |
| R116.2 | B | pin record, and the `plugin_commits` approval wired to git installs (gateway 3aea4d3d). The gateway verifies the grant signature (gateway 738dd8ae, 59b49445). The grant key exists only after the passkey enrolment on alcor (T27) |
| R116.3 | P | `[[needs.tool]]` lists tool and verbs, and the loader and dispatch refuse the rest (gateway ca565b40). Emit subscriptions are guarded by the subscriber's `[[needs.tool]]`, and Python services call with their service token (rust 80518c29, e3265058). Replay test now `test/contract/` (rust 5ab900eb). Left: tokenless callers (cch hooks, the CLI) keep the pid walk |
| R116.4 | B | gateway 29e3b158 (the `trust` key is gone from the contract and every manifest) |
| R116.5 | B | core 48de7a4a |
| R117 | P | item 1 B: tlsfront and dvcdata become libraries, no cross import left (rust 460ba4fb, 5ab900eb). items 2 and 3 B (core 48de7a4a); peer `table_changed` checked in wave 0 (gateway journal 9830ea0b). item 4 B (harness 29adcb67, `harness@1`). item 6 B (libraries, grantlib counts as one). Items 5 and 7 not verified |
| R144 | B | core 48de7a4a (`awm reload`). The install test reloads a changed plugin without a gateway restart (rust a5f2854b) |
| R118 | B | core 21c2b302 (`test/install/run.sh`, rootless). No cloud-agent run yet |
| R119 | P | test images named `quay.io/hallamlab/awm-test-*`, the stubs image gains a cch stub (rust 5ab900eb). Not pushed, no digest pins: T26 |
| R132 | B | core 9fc0da8c, plugins 71f41d9a |
| R133 | B | plugins 2a8fc7ce, 196af229. item 6 placement: cloud `plugins.yml` `engcyc: [altair, showtime]` |
| R134 | P | removals B (plugins 2a8fc7ce; auth v6 3ad110fa). Cognee is out of kb (svc-kb 4857c0f0, on feat/redux since wave 0). kb publishes no MCP tool: its `[[tool]] kb` has tier off (search 538ae158, 06e0597c). kb e997f9b is on altair only and not checked |
| R135 | B | harness ced70632, 0478489f, e93907a0, 7a1a2216, 2e438c1e; core 4ef3593b; plugins 3f897b14. `channel/holders.py` is deleted (channels 84a6b79e), and scopes no longer writes `mcp-opencode.json` (sessions da2c3e90) |

### fleet.md
| R | Status | Where / what is missing |
|---|---|---|
| R79 | P | cloud fleet e7b079a `deneb_station` role. cloud nodes c8d52be `discord_seats` moves the Discord seats to mizar. Not applied |
| R80 | N/A | |
| R81 | B | Ops book is the fixed note `awmSwarmOps` under `awmSwarmBoard`, with a Portals note from the approver's `portals`, no password (cutover 0b22c138). Not applied. The old Ops book is deleted at T27 |
| R82 | P | Penpot dropped (plugins 2a8fc7ce). Game server off through `deneb_station`, not applied |
| R83 | B | fleet 58109656 (Sync now through the approver). Read-only key not checked |
| R84 | P | The approver serves `portal_login`, and httpsfront checks the grant and mints the session (queue 38e4e2b0). The vault login sits behind it (cloud nodes c8d52be `deneb_passkey_login`). Not applied. `approver_portals` lists only nexus. Tony names the other portal hosts at walkthrough M9, and the coordinator writes them at T27 |
| R86 | B | trust 549f8941. gateway b91cc867 deletes the peer book and peer tokens, and peer calls go through the local gateway over the ssh edge. Peer subscribe and lease run over awm-edge with `peer=` (rust 80518c29, e3265058); `fetch_peer_file` dropped (rust 5ab900eb) |
| R87 | B | edge reader (trust 28ffa524, 43ab434a) and cloud `edges.yml`. item 3: alcor's writer reaches the trilium note verbs inside its subtree (queue b664d53a). Not applied |
| R88 | B | stations are in `edges.yml`. item 1: showtime is a station that shogun manages (cloud fleet c06e6e0). Not applied |
| R89 | B | core 6d8cc642 `[[external]]`; cloud `singleton_door` role |
| R92 | B | trust 70a7577c (`awm edge-serve`, `awm port`). gateway b91cc867 deletes the HTTPS node-token transport. Every edge call carries the `peer:<node>` stamp, and the edge streams sockets (rust 80518c29) |
| R93 | B | trust c2d119c2 (`awm-edge`). Not live |
| R94 | P | cloud nodes c8d52be `lyra_setup` (refuses until lyra has a tailnet address) and fleet c06e6e0 admin routes. Not applied. The rest needs Tony at the keyboard (`drafts/edges.md`) |
| R95 | P | cloud `wsl_bridge` role, not applied |
| R96 | P | one network per edge in `edges.yml`. cloud fleet c06e6e0 renders the tailnet policy (item 7), and nodes c8d52be moves capella's Tailscale out of WSL. Not applied. The Tailscale joins need Tony |
| R97 | B | queue d2823c8f (approver, grants, outcomes, cancel by requester); trust 677a3604, 7ce6221a (node-side checks) |
| R98 | B | fleet `alcor_ssh_door` role; trust `awm port`; queue reports over the door. Not applied |
| R99 | P | cloud `singletons_sandbox` role. alcor's units carry memory caps and OOM scores and converge in containers (cloud alcor 356e788). The OOM order is not verified. Not applied |
| R100 | B | trust 7ce6221a (admin ssh grant requests). Not applied |
| R101 | P | cloud `users_sudo` role, not applied |
| R102 | B | trust 47d13807. Tool-name clash with rlm-browser |
| R103 | P | trustpolicy (trust 4c96975c). The gateway passes `_caller_trust` (core e1ec810e, 126afb11). item 2: a failed lookup runs as untrusted and the Rust gateway gates by mode (gateway 9755a706). Gap: a node that loads no reflection resolves every caller trusted, NoOwner (rust a5f2854b), for T23 |
| R104 | B | edges and crates (trust). item 4 B (`awm port` refuses reflection). item 2 B: `policy.first_message_hint`, checked by T12 (sessions journal b729f208) |
| R105 | B | trust 6beec37a (nightly copy, diff hold); cloud `trilium_deneb` role. cloud fleet c06e6e0 `trilium_copy_stop` stops the other copies, and nodes c8d52be `trilium_snapshot` takes the snapshot on deneb (item 4). Not applied |
| R106 | B | queue 416f8f38, f41612a4 |
| R107.1-4 | B | trust cbde2fcf (versions, trust bounds, addenda sweep). Carries the migration and `folded` bugs (§ 2) |
| R107.5-10 | B | journal 9ec42129, dc6953a2, 52894a5e, 66b8690e, 31f07e27. Not done: the `~/.claude` edits (debrief posts `tags`/`reply_to`, goal-alignment, CLAUDE.md precedence lines), AGENTS.md:55 still naming precedence, the one-shots `precedent_import` (altair) and `lineage_import` (each node) |
| R108 | B (config) | cloud fleet 2225de3; converges in containers. Not applied |
| R140 | B | core 1f75dc5a, fleet d744538a, routines 1df74bfc, trust 43ab434a |
| R109.1-6 | B | harness b6394431, c0276daa (rootless container, access-token login). Login-sync unit not installed |
| R109.7 | B | core 8fa54aed, 579dcf87, 65907c3d (token door, whitelist under a ceiling, scope pin). gateway 6c6a28cd looks up the `harness@1` provider, takes the ceiling from the contract and deletes the Python isolated door. The install test passes the door (rust a5f2854b). Open: port 8765 |
| R139 | B | fleet 13ea967a, 295129f8. WoL on alcor untested |
| R131 | P | svc-kb keeps data on disk. papers.db rebuild (sockeye 13884514) pending |
| R136.1-6 | P | altair feat/svc-kb 5cf88c81, 19ffd55e, 7fc61fe9, 4857c0f0, aab968bd; kb a4a2dab, 3d1c391. Hits are title-only and the get log exists. The decayed score `s` and the rank are not built (rank off until the replay gain, by design). In feat/redux since wave 0 |
| R136.7-8 | B | search 538ae158, 06e0597c (sources `name@node` and required, `tags` for `kind`, an optional `hop` object, optional `query`, no `peers`, no precedence source, no kb MCP tool) |
| R90 | B | trust 7ce6221a (invite grant callers); `awm port` logs each call. cloud fleet c06e6e0 renders the tailnet policy that carries the knowledge port between nodes. Not applied |
| R91 | P | cloud nodes c8d52be (`shogun_node`, the Terraform module, `arbutus_infra_move`, `arbutus_backup`) and fleet c06e6e0 (the mizar to shogun edge). Not applied. The VM is created at T27 |
| R120 | M | parked |

### queue.md
| R | Status | Where / what is missing |
|---|---|---|
| R60 | P | new board built. reporting deleted and board cut to the door and its intake (cutover ee2b6037). Board door and intake deletion on `feat/redux-r77` f3928d5f, merges in T27 (R77) |
| R61 | B | queue 0fe170f5, e987f9b7 (Done paging, urgent DM, prompt moves the card) |
| R62, R63 | B | queue 0fe170f5, 2f98b5f7, e987f9b7 |
| R66 | B | one-way writer B. trilium `note_create` takes `note_id`, and alcor's writer reaches the note verbs inside its subtree (queue b664d53a) |
| R64 | B | approver B (queue d2823c8f: passkeys, device chain, windows of 12 h at most). The default address is `auth.tony-xy-liu.com` (queue b664d53a, 38e4e2b0). Host changes in cloud fleet c06e6e0 (SNI passthrough, tunnel user, CSP) and alcor 356e788 (tunnel, cert, a front that reads PROXY). Not applied. The DNS records are Tony's steps (`drafts/manual-steps.md`) |
| R65 | B | queue 2f98b5f7, e987f9b7 (in-flight 60 s). IndexedDB partition not checked on the real names |
| R137 | B | queue 2f98b5f7 |
| R145 | B | queue f41612a4 (one `operations`). operations serves `hook_plan*`, `hook_todos`, `hook_session_clear`, `claim_task`, `track_task`, `swarm_message_post`, `swarm_messages` and the urgent-block reminder (cutover 81c20173); the rest dropped with reasons (cutover journal 115bbbe8). reporting deleted (cutover ee2b6037) |
| R85 | P | enrolment B (2f98b5f7). lyra's key to alcor not done |

### channels.md
| R | Status | Where / what is missing |
|---|---|---|
| R69 | B | one push loop in `awm.gatewayclient.holders`, which channel and seats both import (channels 84a6b79e, 68fe1d31). On Rust, `_caller_pid` is stamped so `holders.caller` names a seat (rust 80518c29) |
| R70 | B | trust tags on seat lines (08660b2e); channel lines carry the level (channels 3a8f7e05); channel admits by level and records `holder_level` (coop e69f17bb) |
| R71 | B | tags B. Levels gated at dispatch by `call_refusal` in awm-trust (coop 1dd271cc), item 3 included; social held-chat rules (coop f1a5535d) |
| R125 | B | Rust gate at dispatch, awm-trust `trust.rs` (coop 1dd271cc): no `start_other`/`resume_other`/`adopt_other`, operations only `request_human`, no trilium, social only held-chat verbs and mount by seat rules (coop f1a5535d), 2fa for cooperative only. 5-subagent cap via cch hook (coop a55f44a9, 65c3b8f1); a slot leaks if SubagentStop never fires. Kept seats start through reflection with a level (coop 9558383a). channel admission by level (coop e69f17bb). A missing ledger entry still reads trusted until the T27 seat restart |
| R127 | B | trust 08660b2e |
| R72 | B | channels 403d003e, b8116164, 3a8f7e05 (`/room/`, task and persistent rooms, six words, holders by R9). Persistent rooms survive a relay restart (tether 67558dd, channels 84a6b79e). Live only after the tether v0.3.0 release and pin bump (T26) |
| R73 | B | channels 6381bb10 |
| R74, R75 | B | met in tether v0.2.0 |
| R76, R138 | P | tether `feat/redux-channels2` af9ea6e at 0.3.0, unpushed and unreleased. The awm pin (cab62d9f) waits on `feat/redux-channels2-pin` for T26 |
| R77 | P | board door, intake and httpsfront's board mount deleted on `feat/redux-r77` f3928d5f (cutover); cutover leaves board door-only with no tool (ee2b6037). Merges in T27, after Caviar and muse move to persistent rooms |
| R110 | B | channels 3a8f7e05 (tether folded in, `awm/services/tether` deleted) |
| R111 | B | channels 84a6b79e (an isolated session holds a channel through its R9 id) |

### users.md
| R | Status | Where / what is missing |
|---|---|---|
| R121 | N/A | |
| R122 | B | trust 549f8941 |
| R123 | B | queue (Arbutus approval path in operations `request_human`) |
| R124 | P | board door still public until `feat/redux-r77` merges in T27 (R77) |
| R126 | B | item 1 B (trust 08660b2e). items 2 and 3: mount below trusted never takes a trusted chat, force only over no holder or the caller's parent (coop f1a5535d, 0922f718). item 4 holds structurally: the gate keys on the recorded level only (coop 1dd271cc). item 5 is judgement in the main seat's brief (docs afcbb936) |
| R128 | P | template (trust 43ab434a). GitHub repo and lab fork not created |
| R129 | B | cloud fleet c06e6e0 `seat_hosts` (Slack on altair, Discord on mizar), nodes c8d52be `discord_seats`, alcor 356e788 social cutover from mira. Not applied |
| R130 | B | trust c22e3533. queue 38e4e2b0, 6cb7ae18 remove every password login (auth schema v7). The auth migration runs at deploy (T26) |

### sessions.md
| R | Status | Where / what is missing |
|---|---|---|
| R9 | B | harness 2b3e0779. Duplicate parser `reporting/federation.py` deleted with reporting (cutover ee2b6037) |
| R56 | B | sessions b3a75403 |
| R58 | B | minted in operations (queue); `reporting/task_ids.py` deleted with reporting (cutover ee2b6037) |
| R57 | B | sessions 86c67200, harness 970b4bf2 |
| R13 | B | journal holds lineage (journal 52894a5e). reflection `start_other` and the harness clear event record the parent (sessions 052f8650) |
| R8 | P | retire draft (sessions 3938718b, 73248bd4). `~/.claude/skills/retire` not updated |
| R10 | B | harness 6f17ffd2. item 3: resume of a retired session from its transcript (sessions b82fdfc1) |
| R146 | B | `start_other` turns Remote Control on by default, named from the session name with each `/` as `-` (sessions 5c7e63d8). Pinned jobs restart after a host reboot (sessions b82fdfc1) |
| R11 | N/A | |
| R12 | P | items 1 to 3 exist. item 4 (decisions to precedent posts) not built |
| R18 | B | sessions b3a75403 `suggest`. cch reads `suggest`, `task_id` and `role` (checked by T12, sessions journal b729f208) |
| R14, R15, R16, R17 | B | sessions 47e3714b; journal 52894a5e, 66b8690e; harness 492f9074 |
| R141 | B | live on release 5453644f |
| R51 | B | harness 6f17ffd2. Cross-node wake tested against a stub only |
| R52 | P | needs R26 deploy live |
| R67 | P | seat-holder skill draft only |
| R68 | B | approver `/panel?task=<id>` selects the card; `task_get` and `request_human` answer `panel_url` (coop ef480704) |
| R78 | B | sessions 6709c060, harness 2e438c1e |

### scopes.md
| R | Status | Where / what is missing |
|---|---|---|
| R1, R40 | B | sessions b3a75403 (lobby `~/.local/state/awm/lobby`) |
| R2 | N/A | |
| R3, R41, R42, R7, R46 | B | sessions 1d7ce46b |
| R4, R43, R5 | B | on release |
| R44 | B | items 1 and 2 (sessions 02531e9f); item 3 (harness 2bc8d552 `_dvc_drift`, stub-tested); item 4 and the discard list from `complete` (sessions 14012be7) |
| R142 | P | harness 9e729b01. sessions da2c3e90 finds a scope by its `.git`, moves the DVC hook state into the git dir, drops the `.awm/data` link and adds `scope_awm_sweep`. Left: the sweep runs once per node at T27, hand-written `.awm/` notes have no one-shot, and the docs change at T20 |
| R143 | P | harness a0ba395c (brief verbs, cch start hook, cd followup); journal 31f07e27. sessions 052f8650 adds the two-sentence rule and sends the brief on a cd of another session. Per-scope briefs are a T27 one-shot |
| R6, R45 | B | sessions d636e610 |

### routines.md
| R | Status | Where / what is missing |
|---|---|---|
| R34, R36-R39, R47, R48 | B | routines 0e9a8840 |
| R19-R21, R23-R25, R27, R29, R30 | B | routines a95afeb1 (engine, batching, two-ref promote, refresh, `full-tests.sh`, dry run 16/16) |
| R26 | B | ticket B. The deploy finds the caller's card and posts the verdict (deploy 366d4d48, e43f70aa) through operations `swarm_message_post` (cutover 81c20173). Not applied |
| R31 | P | `[fleet]` from the install config (1df74bfc). The GitHub backup push and the altair push are built (deploy 366d4d48). Moving canonical from altair to mizar is T27 |
| R32 | B | deploy 366d4d48 (`scripts/refresh-checkout.sh` and the supervised restart prompt) |
| R33 | P | contract `refresh` key. engcyc, rlm-factorio, zotero and claude-science carry a refresh hook (deploy 366d4d48). The split into own projects runs at T27 (cloud nodes c8d52be `plugin_homes`), and the awm folders go at T23 |
| R28, R35 | P | metasmith routine drafted in the routines plan only. R35 waits for a T27 worker on altair, where the metasmith repo lives |

### dispatch.md, parallel-work.md
| R | Status | Where / what is missing |
|---|---|---|
| R49 | N/A | |
| R50 | B | sessions b3a75403 rewrote the brief. The guidelines were checked by T12 (sessions journal b729f208) |
| R59 | P | `drafts/parallel-work/SKILL.md`. Install now unblocked, since R51 is built |

## 4. Remaining work as streams

**Wave 0 (one session, serial). S0, the integration sweep.**
- Merge in the § 2 order onto an integration branch.
- Resolve the listed conflicts.
- Fix every semantic seam in § 2: migrations, the `folded` tag, `peer_gate`/`tagsync` onto edges, `Caller.scope`, `PeerTransport` `/hub/table`, UUID manifests to names, gatewayclient tokens, `_caller_session_id` verbs, the `browser` clash, one task id minter, one grant vector copy, one Pusher.
- Run every suite.
- Every later stream starts from S0's branch.

**Wave 1 (parallel, after S0). Disjoint file sets.**
- **A. Gateway trust and the plugin guard.** Files: `core/crates/awm-gateway`, `awm/gateway`, every `awm-plugin.toml`, `plugin-contract.md`.
  - R103 item 2: untrusted default, and a mode gate in Rust.
  - R116 item 3: verbs in `[[needs.tool]]`, enforced per token or page path.
  - R116 item 4: drop `trust = own|third-party`.
  - R116 items 1 and 2: network, git install, wiring of operations `plugin_commits`.
  - R117 item 3: peer `table_changed`.
- **B. Search R136 items 7 and 8, and kb tool removal (R134).** Files: `awm/services/search`, kb plugin; altair `feat/svc-kb` and kb `feat/shards`. Needs S0 for journal tags and the precedent fold. Merge svc-kb into the S0 branch first; its conflicts are unknown from mizar. The papers.db ship (R131) runs alongside.
- **C. Deploy route.** Files: `awm/plugins/routine`, `deploy.toml`.
  - R31 and R114: mizar canonical, GitHub backup push.
  - R26: card verdict through operations.
  - R32: supervised restart.
  - R35: the metasmith routine in the metasmith repo.
  - The repo move itself is a manual step.
- **D. Fleet config.** Files: cloud `homelab/`, plus one awm default.
  - R88 item 1: showtime in `edges.yml`.
  - R64: `auth.tony-xy-liu.com` in cloud `approver.yml` and `operations/cli.py`.
  - The values from question 10.
  - The R129 placement.
  - The queue host-change list.
  - R94, R96 and R91 at Tony's keyboard.
- **E. Channels.** Files: tether repo, `awm/plugins/channel`, `social/seats.py`.
  - Release tether and bump the pin (R76, R138).
  - R69 shared push loop. Needs S0, which brings trust's `seats.py`.
  - R111 isolated-session channels.
  - R72 persistent rooms that survive a relay restart, if wanted.
- **F. Sessions and harness leftovers.** Files: `awm/services/{cch,reflection,scopes,journal}`.
  - R13: reflection records parents.
  - R10 item 3: resume from the transcript.
  - R143: the two-sentence rule and cd of another session.
  - R44 item 4.
  - R142: DVC hook state.
  - R18: verify cch reads `suggest`, `task_id` and `role`.
- **G1. Queue fixes.** Files: `awm/plugins/operations`, `awm/services/trilium`.
  - trilium `note_create` with `note_id`.
  - Cross-edge trilium verbs for alcor's writer (R87 item 3).
  - PROXY protocol.
  - The `order_pull` caller check.
- **K. `~/.claude` and workspace docs.** Files outside the awm repo, plus AGENTS.md.
  - debrief posts `tags` and `reply_to`.
  - The retire skill ends with `reflection stop` (R8).
  - Install parallel-work (R59) and the seat-holder SOP (R67).
  - goal-alignment writes an ordinary post.
  - CLAUDE.md drops graphify and precedence, and points at precedent posts.
  - AGENTS.md drops OpenCode, precedence and graphify.
  - Land K with the deploy.

**Wave 2 (needs wave 1 results).**
- **E2. R125 cooperative gating, enforced in the gateway.** Needs A.
- **G2. Queue cutover (R60, R145).**
  - Port the remaining reporting verbs to operations.
  - Remove reporting and board.
  - R77: remove the board door and intake once Caviar and muse run on persistent rooms. Needs E.
  - The trilium tarball and the fork move to mizar (R115 item 4).
- **H. Rust core goes live (R112).**
  - Run the services on the Rust gateway.
  - Remove the legacy loader paths and the `/svc` door.
  - Remove the cross imports (R117 item 1).
  - Push the images to quay and pin them by digest (R119).
  - Needs A.

**Wave 3 (serial).**
- **I. Cleanup pass.** The lists in each review README § "For the cleanup pass".
- **J. Deploy sweep.**
  - auth 5 to 6 on altair.
  - `precedent_import` on altair, `lineage_import` on each node.
  - Write each scope's brief, then `scope heal` and `deploy-hooks.sh`.
  - The journal moves to its own user (`/var/lib/awm/journal`).
  - Ansible apply (R108), the host changes, and a cross-node wake check (R51).
  - Needs C (canonical on mizar) and K.

## 5. Gathers

- **Wave 0, 2026-11-04.** `feat/redux` 3057f006 and 2917f323 merge `feat/redux-integrate` 0474918c (journal 08e9d3f0). It holds every lane in § 1 and `feat/svc-kb` aab968bd. Its diff from 0fef1ad4 is +52,772 and −126,025 lines over 1,105 files. 41 Python dists pass (channel needs spake2 in the env), operations passes 104 in the awm-queue env, and `cargo test --workspace` passes. 7 Slack tests skip without slack_sdk. Every T1 Delete and Modify item is done. The lane sessions redux-journal, redux-plugins, redux-channels and redux-queue are retired, with their worktrees and branches kept.
- Open after wave 0: the Python loader refuses operations and browser while board still serves `operations@1` (R145's cutover state, closed by T18). `caller.rs` `r9` goes in T6.
- **Wave 1, 2026-11-04.** `feat/redux` fast-forwards to gateway 61b31e89 (T6, journal 9830ea0b), then to sessions 6daad8bf (T12, journals b729f208 and 31373881). Then it merges queue 6cb7ae18 (T13, journal 9929bd81) as bd84bcca, channels 68fe1d31 (T11, journal 7394e430) as 6fcbed58, search 06e0597c (T7, journal d691d982) as d31271b3, and deploy e43f70aa (T8, journal de0d3454) as af854b5b. Each later branch merged gateway's plugin contract v1.10 into itself first. The diff from 337fa145 is +6,812 and −15,055 lines over 365 files, and +6,311 and −13,618 in Python and Rust. 41 Python dists pass, operations passes 112 in the awm-queue env, and the Rust workspace passes. 38 dvc property tests skip on mizar, which has no dvc. Cloud `feat/redux-fleet` merges fleet c06e6e0 (T9, journal b14db7d8), nodes c8d52be (T10, journal 91527b67) and alcor 356e788 (T2, journal 6b7f041d) as b2f5e42. No role is applied to a host.
- Cloud `feat/redux-fleet` then fast-forwards to 88b6747 (journal c0c3eaf5): one list of the playbooks that may target mira, one `move_stop` gate for every stop, deneb's `awm_dir` under /opt/awm, and the dead peer and mesh keys removed. pytest, run-containers.sh, run-moves.sh and trust/run.sh pass.
- Open after wave 1: the avatar relay still relies on the fleet CA that R92 retires. The tether v0.3.0 pin waits on `feat/redux-channels2-pin` for T26. R35 waits for T27 on altair. The cross-node subscribe, lease and file fetch refuse until T19.
- **Wave 2, 2026-11-04.** `feat/redux` merges coop 0922f718 (T17, journal bb9a4d4d) as b0bf1311, cutover 0b22c138 (T18, journal 115bbbe8) as da1fdba5, and rust a5f2854b (T19, journal 9034edd4) as 07ca00ce, then rust's follow-up a4494351 as 0481f425. The diff from 1408a0d9 is +7,284 and −43,122 lines over 481 files, and +6,045 and −40,798 in Python and Rust. The Python gateway, reporting and the legacy loader are gone. 41 Python dists pass, operations passes 140 in the awm-queue env, the Rust workspace passes 69, and the install test passes with R144 hot reload and the R109 item 7 door. `feat/redux-r77` f3928d5f holds the board and intake deletion for T27.
- Open after wave 2: a node that loads no reflection resolves every caller trusted (T23). A missing seat ledger entry reads trusted until the T27 seat restart. The subagent cap leaks a slot when SubagentStop never fires (T23). MCP `tools/list_changed` is not ported (T23). `awm deploy`'s apply path first runs at the first node deploy. The avatar relay moves off the fleet CA in cloud (redux-build/alcor), and the avatar stage client changes on capella (M10). The Trilium tarball host is open with Tony.

## 6. Deferred

- **R120** (Armando's machines) waits for Tony's meeting with Armando.
- **The Slack-only Arbutus box** and **the three lost altair sessions** stay parked inputs. Neither has a requirement.
