# Redux build lanes

Coordinator: the interview session, job 8162a704 on mizar. Tony: "You are coordinator" (2026-11-03).

| Session | Job | Scope | Owns |
|---|---|---|---|
| redux-build/core | a92d02bf | awm/redux-core | install.md R112 to R119, R132 gateway side: build container, Rust core, CLI, gateway, plugin loader, MCP proxy |
| redux-build/harness | b7312951 | awm/redux-harness | thread 7 compaction failures, install.md R135, sessions.md R10 and R51, fleet.md R109 isolated sessions |
| redux-build/plugins | b4dbaa5a | awm/redux-plugins | install.md R115 to R117 adoption by each service, R132 plugin side, R133, R134 |
| redux-build/fleet | 061af0f6 | awm/redux-fleet | fleet.md R139 wake and standby, R98, R79 to R84 deneb hardening as config, R108 config pass, R120 parked |
| redux-build/channels | 5a3ff6e5 | awm/redux-channels | channels.md all, R138 tether shell, R110 and R111 one channel tool, rooms, tether |
| redux-build/sessions | 2ec1b21d | awm/redux-sessions | sessions.md except R10 and R51, scopes.md, dispatch.md, parallel-work.md |
| redux-build/routines | 1303f97b | awm/redux-routines | routines.md: routines, CI, deploy |
| redux-build/queue | 593a3cae | awm/redux-queue | queue.md all, R137 2fa buttons, fleet.md R106 task queue and R97 approvals |
| redux-build/trust | 563d3f5a | awm/redux-trust | fleet.md R86 to R105 and R107 edges, singletons, grants, trust per session; users.md R121 to R130 seats and users |
| search-finish | 6a450f72 on altair | awm/svc-kb | fleet.md R136 new search, kb's search parts |

Moves on 2026-11-03: R138 from fleet to channels, R137 from fleet to queue.

R140 on 2026-11-03: awm is the product and names no host. The cloud project holds the fleet: Ansible (R108), node firewall, ssh, users and sudo, and plugin placement. redux-build/fleet writes the config pass in a cloud scope (project cloud, scope redux-fleet). redux-build/trust sends its node config there.

## Status
- redux-build/routines: done at 1df74bfc on feat/redux-routines (routine plugin, deploy engine, dry run 16/16, metasmith release routine draft). R140 follow-up done: the engine reads home base, rollout order and canonical nodes from [fleet] in <AWM_DIR>/config.toml. deploy.toml names no host. 69 tests pass. Stopped. Callouts: deploy.toml's node list is unverified, the old schedulers move only after go-live, and the gateway smoke-tier fix lives on feat/redux-core.
- redux-build/plugins: phase 1 done (196af229 virtmic fold, 2a8fc7ce removals, 0692f53f discovery reads awm-plugin.toml). Next: manifests and guards, then usage.
- redux-build/harness: compaction fix at 38a8c1b1 on feat/redux-harness (420 reflection tests pass). Owed resumes stay on disk until the transcript shows them taken up. A worker swap is followed to the new pid and retried up to twice. Each give-up is loud: an ERROR log, 'lost' in reflection pending, a journal message and a line into the session. Next: the cch and dsh recut.
- redux-build/core: done at caa5451f on feat/redux-core. Built: the build container, the Rust core (gateway, loader, MCP proxy, CLI, bootstrap) and usage per plugin. The e2e test is green. It covers a fake plugin, a third-party plugin, the unchanged Python journal and an external service, through `awm mcp` and the CLI. Contract v1.5 is at docs/redux/drafts/plugin-contract.md. Journal 8bfea6d9. Retired. Run resume_other a92d02bf for a contract change.
- redux-build/queue: done at 0fe170f5 on feat/redux-queue. Built: the task store, the relay, the approver (passkeys, device chain, grants, orders), the panel with its 2fa strip, and the Trilium board page with its writer and the spawn body check. 68 tests pass, including Chromium runs against a real Trilium. Seven host changes for the sweep (Cloudflare DNS-only and CAA, deneb SNI passthrough, CSP, alcor tunnel unit, lyra key to alcor, deneb ETAPI token) are in docs/redux/drafts/redux-queue-plan.md § Callouts. Stopped.
- redux-build/channels: rooms work end to end (awm 403d003e). The board door and intake deletion (T9) is held on question 8. Now on T10 and T11.
- redux-build/sessions: done at 9ca2ce61 on feat/redux-sessions (13 commits). It merged origin/dev for the keeper witness fix, so the journal schema is now v3. R78 adds the keeper online gate and local_only. Journal e105037a. Open: question 6 (R44). The parallel-work skill install waits on harness's reflection wait. The sweep must merge social with feat/redux-trust 08660b2e. Stopped.
- redux-build/channels: done (awm feat/redux-channels bbc0b926, tether feat/redux-channels a046742). Built: the R138 launcher, the R76 random slot, relay rooms with their edge routes, and the channel tool. 36 plugin tests and 68 gateway identity tests pass. T9 is held on question 8. Journal 110b35e2. Stopped.
- redux-build/plugins: done at 3db13252 on feat/redux-plugins. R134 removals. R133 kept set with no host names. Manifests for 23 folders and a guard test, with 7 legacy folders kept for stated reasons. R132 plugin side: awm.usage, the Python gateway sink and emitters in transcripts, rlm-factorio and mic. Green apart from the 10 baseline failures. Journal 77a7987c. Retired.
- redux-build/trust: done. awm feat/redux-trust head 5ae958f1: edges replace foreign/domestic, the awm-trust and awm-edge crates, trustpolicy, grant callers passing queue's vectors, browser seats, the journal edits, bounds and addenda sweep, auth without static users, Slack send-as-Tony removed, the first-DM gate, agentic-commons and the Trilium backup. cloud feat/redux-trust a547baa: node roles, tested in containers, merging into fleet. Stopped.
| redux-build/core-caller | ac9bdf04 | awm/redux-core | the gateway passes _caller_trust and _caller_scope (Python and Rust), for redux-build/trust. It replaces a92d02bf, because resume_other refused that job |
- redux-build/harness: done through c0276daa on feat/redux-harness. Built: the compaction fix, the cch and dsh recut, services moved off claudedaemon, wait and resumable retire, isolated sessions, and proposed doc changes. Journal f8e708f9. Sweep: the gateway token door (with core-caller), building the image and installing the login-sync unit, and a pass on cch/INSTALL.md. Stopped.
- redux-build/core-caller: done at 126afb11 on feat/redux-core. Both gateways now pass _caller_trust and _caller_scope (contract v1.6). 1134 Python gateway tests and cargo test pass. The sweep must add `scope` to 4 Caller literals when it merges feat/redux-trust; the list is in redux-core-plan.md § Callouts.
| redux-build/core-isolation | a0070cc9 | awm/redux-core | the gateway door for isolated sessions (X-Awm-Caller-Token, whitelist, scope pinning, MCP door for containers), for redux-harness R109 |
- redux-build/core-isolation: done at 65907c3d on feat/redux-core. The isolated-session door is built in both gateways: POST /mcp with X-Awm-Caller-Token, a whitelist under a fixed ceiling, this node only, scope pinned, and a door-only listener on 8765. The tests pass and the merges with harness and trust are clean. Journal 9bed69fc. Stopped.
- redux-build/fleet: done. awm feat/redux-fleet: the wake plugin (13ea967a, d744538a, 295129f8; 27 tests; names no host) and the zotero Sync now puller (58109656). cloud feat/redux-fleet homelab/ (3d33b1d to 2225de3): the R108 config pass with trust's roles merged. site.yml converges on fresh mizar, alcor and deneb containers, and a second run reports changed=0. Stopped.
