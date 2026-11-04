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
| R112 | P | Rust core built (core 759e516a, 1f75dc5a). No service runs on it. Every node runs Python |
| R113 | B | core ae8306d5, 8c678557 |
| R114 | S | 11-04: GitHub is a backup of mizar's canonical repo, and each deploy pushes `release` to it. Not built: origin is still altair, and no deploy code pushes to GitHub (grep of `awm/plugins/routine`: none) |
| R115 | P | Folder install with manifests and pins built (plugins 503291d8, 14639f7b; core 341dd669 `installed.toml`). Missing: the git source (seam refuses), optional plugins as their own projects, the trilium tarball install with the fork on mizar, and pages still built into `awm/pages` |
| R116.1 | P | secrets only (core 341dd669). network is not enforced |
| R116.2 | P | pin record and approval hook as a refusing seam (core 341dd669). operations has a `plugin_commits` kind (queue), not wired to install |
| R116.3 | S/M | 11-04: `[[needs.tool]]` lists tool and verb, and the gateway refuses the rest, keyed by service token or page path. Today it checks only presence and major, at install (contract v1.9) |
| R116.4 | S | 11-04: installing trusts the plugin. The contract still carries `trust = "own" \| "third-party"` (`plugin-contract.md:27`). Remove it |
| R116.5 | B | core 48de7a4a |
| R117 | P | items 2 and 3 B (core 48de7a4a); item 4 B (harness 29adcb67, `harness@1`). item 1: cross imports remain (claude-science and dsh to httpsfront, reporting to board, scopes to dvc). item 3: peer `table_changed` over the edge is not wired. Items 5 to 7 not verified |
| R144 | B | core 48de7a4a (`awm reload`) |
| R118 | B | core 21c2b302 (`test/install/run.sh`, rootless). No cloud-agent run yet |
| R119 | P | images built as `quay.io/hallamlab/awm-test-*:dev`, not pushed. Org is an open question. No digest pins |
| R132 | B | core 9fc0da8c, plugins 71f41d9a |
| R133 | B | plugins 2a8fc7ce, 196af229. item 6 placement: cloud `plugins.yml` `engcyc: [altair, showtime]` |
| R134 | P/S | removals B (plugins 2a8fc7ce; auth v6 3ad110fa). Cognee out of kb only on altair (svc-kb 4857c0f0, kb e997f9b). 11-04: the `kb` tool goes. It is still present on every lane |
| R135 | B | harness ced70632, 0478489f, e93907a0, 7a1a2216, 2e438c1e; core 4ef3593b; plugins 3f897b14. Leftovers: the claudedaemon fallback in `channel/holders.py`, `scopes.py:507,522,604` writing `mcp-opencode.json`, and OpenCode in AGENTS.md |

### fleet.md
| R | Status | Where / what is missing |
|---|---|---|
| R79 | P | cloud fleet e7b079a `deneb_station` role, not applied. Discord seats still on deneb |
| R80 | N/A | |
| R81 | M | no lane reports the Ops page change |
| R82 | P | Penpot dropped (plugins 2a8fc7ce). Game server off through `deneb_station`, not applied |
| R83 | B | fleet 58109656 (Sync now through the approver). Read-only key not checked |
| R84 | P | approver login pages (queue 2f98b5f7). The vault and portal passkey logins are not wired or applied |
| R86 | B | trust 549f8941 |
| R87 | P | edge reader (trust 28ffa524, 43ab434a) and cloud `edges.yml`. item 3: the trilium note verbs are operator-only and refuse a cross-edge call, so alcor's board writer is refused (queue report) |
| R88 | P/S | stations are in `edges.yml`. item 1 (11-04, showtime is a station) M: showtime is absent from `edges.yml` |
| R89 | B | core 6d8cc642 `[[external]]`; cloud `singleton_door` role |
| R92 | B | trust 70a7577c (`awm edge-serve`, `awm port`). The HTTPS node-token transport stays until the Rust gateway takes peer calls |
| R93 | B | trust c2d119c2 (`awm-edge`). Not live |
| R94 | M | needs Tony at the keyboard (`drafts/edges.md`) |
| R95 | P | cloud `wsl_bridge` role, not applied |
| R96 | P | one network per edge in `edges.yml`. The Tailscale moves need Tony |
| R97 | B | queue d2823c8f (approver, grants, outcomes, cancel by requester); trust 677a3604, 7ce6221a (node-side checks) |
| R98 | B | fleet `alcor_ssh_door` role; trust `awm port`; queue reports over the door. Not applied |
| R99 | P | cloud `singletons_sandbox` role. Memory caps and the OOM order not verified. Not applied |
| R100 | B | trust 7ce6221a (admin ssh grant requests). Not applied |
| R101 | P | cloud `users_sudo` role, not applied |
| R102 | B | trust 47d13807. Tool-name clash with rlm-browser |
| R103 | S | trustpolicy (trust 4c96975c). Gateways pass `_caller_trust` (core e1ec810e, 126afb11). item 2 (11-04): a failed lookup must run as untrusted, but both gateways default to trusted (`core/.../caller.rs:16` `DEFAULT_TRUST = "trusted"`, `gateway/mcp_caller.py:35`). The Rust gateway has no mode gate |
| R104 | P | edges and crates (trust). item 4 B (`awm port` refuses reflection). item 2 (the first local message points to the harness tool) not reported |
| R105 | P | trust 6beec37a (nightly copy, diff hold); cloud `trilium_deneb` role. Not done: stopping the other vault copies, item 4 (altair's snapshot moves to deneb) |
| R106 | B | queue 416f8f38, f41612a4 |
| R107.1-4 | B | trust cbde2fcf (versions, trust bounds, addenda sweep). Carries the migration and `folded` bugs (§ 2) |
| R107.5-10 | B | journal 9ec42129, dc6953a2, 52894a5e, 66b8690e, 31f07e27. Not done: the `~/.claude` edits (debrief posts `tags`/`reply_to`, goal-alignment, CLAUDE.md precedence lines), AGENTS.md:55 still naming precedence, the one-shots `precedent_import` (altair) and `lineage_import` (each node) |
| R108 | B (config) | cloud fleet 2225de3; converges in containers. Not applied |
| R140 | B | core 1f75dc5a, fleet d744538a, routines 1df74bfc, trust 43ab434a |
| R109.1-6 | B | harness b6394431, c0276daa (rootless container, access-token login). Login-sync unit not installed |
| R109.7 | B | core 8fa54aed, 579dcf87, 65907c3d (token door, whitelist under a ceiling, scope pin). Open: port 8765, the ceiling list, `isolated.rs:18` hard-codes `harness-cc` |
| R139 | B | fleet 13ea967a, 295129f8. WoL on alcor untested |
| R131 | P | svc-kb keeps data on disk. papers.db rebuild (sockeye 13884514) pending |
| R136.1-6 | P | altair feat/svc-kb 5cf88c81, 19ffd55e, 7fc61fe9, 4857c0f0, aab968bd; kb a4a2dab, 3d1c391. Hits are title-only and the get log exists. The decayed score `s` and the rank are not built (rank off until the replay gain, by design). Not merged into any mizar lane |
| R136.7-8 | S/M | 11-04: sources carry `@node`, sources are required, the `peers` arg goes, tags replace `kind`, the precedence source goes, an optional `hop` JSON object, optional `query`, the kb tool goes. As built on 11-04: bare sources, `peers`, one `kind`, no hop |
| R90 | P | trust 7ce6221a (invite grant callers); `awm port` logs each call. Not done: the cross-network Tailscale route |
| R91 | M | shogun, needs Tony |
| R120 | M | parked |

### queue.md
| R | Status | Where / what is missing |
|---|---|---|
| R60 | P | new board built. Removing reporting and board waits on cutover |
| R61 | B | queue 0fe170f5, e987f9b7 (Done paging, urgent DM, prompt moves the card) |
| R62, R63 | B | queue 0fe170f5, 2f98b5f7, e987f9b7 |
| R66 | P | one-way writer B. trilium `note_create` takes no `note_id`, and the note verbs refuse a cross-edge call |
| R64 | S | approver B (queue d2823c8f: passkeys, device chain, windows of 12 h at most). 11-04: the address is `auth.tony-xy-liu.com`, but the code default is `approve.tony-xy-liu.com` (`operations/cli.py:35`, cloud `group_vars/all/approver.yml:3`). Host changes not applied: DNS-only, CAA, deneb SNI passthrough, tunnel unit, CSP. PROXY protocol missing |
| R65 | B | queue 2f98b5f7, e987f9b7 (in-flight 60 s). IndexedDB partition not checked on the real names |
| R137 | B | queue 2f98b5f7 |
| R145 | P | queue f41612a4 (one `operations`). Not served yet: reporting verbs (`hook_plan*`, `hook_todos`, `hook_session_clear`, `claim_task`, `track_task`, `board_*`, `swarm_*`, `convert`, `peer_*`) and the urgent-block reminder |
| R85 | P | enrolment B (2f98b5f7). lyra's key to alcor not done |

### channels.md
| R | Status | Where / what is missing |
|---|---|---|
| R69 | P | the channel loop copies social's Pusher. The shared loop waits on trust's `seats.py` |
| R70 | B | trust tags on seat lines (08660b2e); channel lines carry the level (channels 3a8f7e05) |
| R71 | P | tags B. The behaviour rests on prompts plus trustpolicy |
| R125 | P | trustpolicy library only. No gateway gate (social block, no top-level start). channel admits any background session |
| R127 | B | trust 08660b2e |
| R72 | B | channels 403d003e, b8116164, 3a8f7e05 (`/room/`, task and persistent rooms, six words, holders by R9). Live only after the tether release and pin bump. A relay restart ends persistent rooms |
| R73 | B | channels 6381bb10 |
| R74, R75 | B | met in tether v0.2.0 |
| R76, R138 | P | tether `feat/redux-channels` 94a30f2, unpushed and unreleased |
| R77 | M | board door and intake deletion (T9) not done. Waits on Caviar and muse moving to persistent rooms |
| R110 | B | channels 3a8f7e05 (tether folded in, `awm/services/tether` deleted) |
| R111 | P | an isolated session cannot hold a channel (no job to push into) |

### users.md
| R | Status | Where / what is missing |
|---|---|---|
| R121 | N/A | |
| R122 | B | trust 549f8941 |
| R123 | B | queue (Arbutus approval path in operations `request_human`) |
| R124 | P | board door still public (R77) |
| R126 | P | item 1 B (trust 08660b2e). Items 2 to 5 not reported |
| R128 | P | template (trust 43ab434a). GitHub repo and lab fork not created |
| R129 | M | seats and social not moved. Placement not verified |
| R130 | B | trust c22e3533. altair's auth 5 to 6 migration runs at deploy |

### sessions.md
| R | Status | Where / what is missing |
|---|---|---|
| R9 | B | harness 2b3e0779. Duplicate parsers: `reporting/federation.py`, `queue-relay/ids.py` |
| R56 | B | sessions b3a75403 |
| R58 | P | minted in operations (queue) and `reporting/task_ids.py` (sessions 86c67200). Keep one |
| R57 | B | sessions 86c67200, harness 970b4bf2 |
| R13 | P | journal holds lineage (journal 52894a5e). reflection does not yet record parents through `journal session_parent` (cch `lifecycle.py:316` and reporting `tasks.py:538` do today) |
| R8 | P | retire draft (sessions 3938718b, 73248bd4). `~/.claude/skills/retire` not updated |
| R10 | P | harness 6f17ffd2. item 3 (resume a retired session from its transcript) M |
| R11 | N/A | |
| R12 | P | items 1 to 3 exist. item 4 (decisions to precedent posts) not built |
| R18 | P | sessions b3a75403 `suggest`. Whether cch reads `suggest`, `task_id` and `role` after the cx-to-cch move needs a check |
| R14, R15, R16, R17 | B | sessions 47e3714b; journal 52894a5e, 66b8690e; harness 492f9074 |
| R141 | B | live on release 5453644f |
| R51 | B | harness 6f17ffd2. Cross-node wake tested against a stub only |
| R52 | P | needs R26 deploy live |
| R67 | P | seat-holder skill draft only |
| R68 | P | panel exists. No flow sends a seat to the card link |
| R78 | B | sessions 6709c060, harness 2e438c1e |

### scopes.md
| R | Status | Where / what is missing |
|---|---|---|
| R1, R40 | B | sessions b3a75403 (lobby `~/.local/state/awm/lobby`) |
| R2 | N/A | |
| R3, R41, R42, R7, R46 | B | sessions 1d7ce46b |
| R4, R43, R5 | B | on release |
| R44 | P | items 1 and 2 (sessions 02531e9f); item 3 (harness 2bc8d552 `_dvc_drift`, stub-tested). item 4 (block when the branch is kept, `scopes.py:414`) M. `complete` drops the discard list |
| R142 | P | harness 9e729b01 (render writes no file, heal deletes stale files). The `.awm/data` link and the DVC hook state not moved |
| R143 | P | harness a0ba395c (brief verbs, cch start hook, cd followup); journal 31f07e27. The two-sentence rule is not in code. A cd of another session carries no brief. Per-scope briefs are a deploy step |
| R6, R45 | B | sessions d636e610 |

### routines.md
| R | Status | Where / what is missing |
|---|---|---|
| R34, R36-R39, R47, R48 | B | routines 0e9a8840 |
| R19-R21, R23-R25, R27, R29, R30 | B | routines a95afeb1 (engine, batching, two-ref promote, refresh, `full-tests.sh`, dry run 16/16) |
| R26 | P | ticket B. The card half of the verdict is a stub; operations can now carry it |
| R31 | S | `[fleet]` from the install config (1df74bfc). 11-04: mizar canonical plus a GitHub backup push each deploy, not built. Canonical is still altair |
| R32 | P | `queue` refresh kind only |
| R33 | P | contract `refresh` key. Optional plugins not split out |
| R28, R35 | P | metasmith routine drafted in the routines plan only |

### dispatch.md, parallel-work.md
| R | Status | Where / what is missing |
|---|---|---|
| R49 | N/A | |
| R50 | P | brief guidelines not verified (sessions b3a75403 rewrote the brief) |
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

## 6. Deferred

- **R120** (Armando's machines) waits for Tony's meeting with Armando.
- **The Slack-only Arbutus box** and **the three lost altair sessions** stay parked inputs. Neither has a requirement.
