# Plan: redux-trust

## Context

The redux build runs one session per lane. This lane covers how nodes talk to each other, how much a session is trusted, and who uses awm through which door: the edge list, the singleton door, admin grants, trust per session, browser seats, the journal as its own service, and the Slack seat system. Tonight it builds code and writes node config. It applies nothing to a host and moves nothing off mira.

## What you said

> Your lane: communication between nodes, trust, and users. Read fleet.md R86 to R105 and R107, users.md R121 to R130, channels.md R125 and R127, and the gap inventory rows for them.

> the directional edge list that matches ssh in place of foreign and domestic peers (delete the foreign/domestic code), one network per edge, singletons on alcor behind their own door, admin grants in place of sudo, trust per session, browser seats, journals as a service, and the Slack seat system: trust gates, removing the Slack send-as-Tony function, no first DM in code, the agentic-commons workspace with local git repos per seat, the rt user, and removing altair's static users.

> Write node config (ssh, firewall, users, sudo) as config for the fleet's config pass that redux-fleet (job 061af0f6) owns (fleet.md R108). Apply none of it. Moving social or the singletons to alcor waits on the mira trial week (fleet.md R88 item 1, R129): build the code, move nothing. Isolated sessions (R109) belong to redux-harness (job b7312951).

> Local updates only, bring the peers up to date in one sweep when we are done

## Issues

- I1. Each node keeps a hand-written peer book with domestic and foreign relations, swarm names and read grants, and the books disagree (F14, F16, F22, U5).
- I2. No single record states which node reaches which, over which network, so ssh keys, ssh routes and awm's peer calls drift apart (F21, F17, R87, R96).
- I3. A cross-node call rides HTTPS with node tokens and a private CA, and no node logs who called it (F13, F18, F30).
- I4. Singletons live on mira behind env selectors, with no door of their own and no wall between them and compute (F20, R98, R99).
- I5. Admin work runs through passwordless sudo on the normal account and hand-opened gates (F10, F41, R100, R101).
- I6. No session carries a trust level, so nothing stops a seat or an agent from acting above its standing (C16, C18, R103, R104).
- I7. The browser is one shared Chrome on mira and capella with an open debug port, not one window per seat (F19, F35, F38, R102).
- I8. The journal is one more awm service: a post cannot be corrected in place, and every session reads every journal (R107).
- I9. `social` can send on Tony's own Slack account, can DM a person first, and gives a seat every chat's history (C17, U2, U11).
- I10. Seats live in metasmith and awm scopes, and no seat workspace exists (U3, U12).
- I11. altair's HTTPS edge holds static per-user passwords (U8, R130).
- I12. The rt jobs share an account, and Trilium runs on several nodes with unauthenticated sync (R89 item 1, R105).

## High-level goals

- G1. Replace the foreign and domestic system with one directional edge list that matches ssh, and leave nothing of the old system behind.
- G2. Carry each cross-node call over the ssh edge, one network per edge, with warm connections and a log of every service call.
- G3. Put the singletons behind their own door on alcor, walled off from compute, ready to move once the mira trial week ends.
- G4. Make every admin login and every trust raise go through a grant Tony approves, so normal accounts need no sudo.
- G5. Give every session a trust level that awm sets and tracks and that no agent can raise.
- G6. Give each seat its own browser window, with Tony's profile only for trusted sessions.
- G7. Make the journal its own service with corrections kept as diffs and reads bounded by trust.
- G8. Make the Slack seat system safe by construction: no writing as Tony, no first DM, seats bounded to their own chats, and a seat workspace of its own.
- G9. Hand the fleet's config pass every node change this lane needs, as config it can apply in one sweep.

## Acceptance criteria

- No `relation`, `domestic`, `foreign`, `swarm` or `grants` field remains in the peer book reader, the gateway, `gatewayclient`, `httpsfront` or the surviving services. The board is excepted (redux-channels deletes it, C15). The touched tests pass.
- The cloud project's `homelab/edges.yml` holds the R87 table exactly, each node's role (R88), one network per edge (R96) and each singleton host's services (R89, R98). A test checks the table against the R87 rows.
- `core/crates/awm-trust` and `core/crates/awm-edge` build and pass their tests in mizar's build container. `awm-edge` implements core's `PeerTransport` trait.
- `awm port`, the forced command of a service key, refuses every verb outside its set and appends one log line per call: time, key comment, verb, result.
- The trust library refuses a set above the starter's level, refuses a raise without a valid `trust_raise` grant, and refuses start or steer from an untrusted session. Tests cover each.
- The journal stores an edit as a diff, reads return the latest version, history returns every version, and a read outside the caller's trust bound is refused. Tests cover each level.
- No code path in `social` sends on Tony's own Slack account. A send verb that names it fails because the function does not exist.
- `slack_bot_conn` refuses a DM to a person with no message in that DM. A test covers it.
- A seat's `social history` returns only that seat's chats.
- The browser seat code gives each seat its own window and confines its verbs to it. A non-trusted seat gets no profile of Tony's.
- The cloud project's `homelab/roles/` holds `ssh_edges`, `users_sudo`, `singleton_door`, `singletons_sandbox`, `rt_user`, `journal_user`, `trilium_deneb` and `wsl_bridge`. `ansible-playbook --check --syntax-check` passes. No role targets mira.
- `awm auth user-add` and the static per-user password store are gone. A config task removes altair's two users.
- Every commit sits on `feat/redux-trust` only.

## Tasks

- T1. Agree the interfaces with the sibling lanes (G1, G2, G4, G5, G9).
- T2. Write the edge list as data, with a reader in Python (G1, G2, G9).
- T3. Delete the foreign and domestic code and replace each check with an edge check (G1).
- T4. Commit a checkpoint.
- T5. Compact.
- T6. Remove the Slack send as Tony, block the first DM, and bound each seat to its own chats (G8).
- T7. Scaffold the agentic-commons workspace and the seat folder initialiser (G8).
- T8. Remove the static portal users (G8, G9).
- T9. Commit a checkpoint.
- T10. Make the journal its own service with diff edits and trust-bounded reads (G7).
- T11. Build the trust policy library (G5).
- T12. Commit a checkpoint.
- T13. Compact.
- T14. Build the Rust crates `awm-trust` and `awm-edge` and the service door `awm port` (G2, G3, G5).
- T15. Build the grant request callers and node-side grant checks (G4).
- T16. Build the browser seat singleton (G6).
- T17. Build the Trilium backup pull and diff check (G9).
- T18. Commit a checkpoint.
- T19. Compact.
- T20. Write the node config roles for the fleet pass (G3, G4, G9).
- T21. Draft the doc updates in this plan file.
- T22. Commit and report to the coordinator.
- T23. Debrief.

## Approach by task

### T1. Agree the interfaces with the sibling lanes

Settled on 2026-11-03 by message:

- **redux-core (a92d02bf).** The cargo workspace is `core/` with `crates/*`. This lane adds `core/crates/awm-trust` and `core/crates/awm-edge`. `awm-edge` implements core's `PeerTransport` trait and depends on `awm-proto` for `CallContext { caller_node, lane, trust, as_, surface }`. Core sends the build container command when it is up. Asked: the gateway strips any caller-sent trust header and fills the call frame's caller context itself.
- **redux-fleet (061af0f6).** Roles go under `fleet/roles/<role>/`, one `import_role` line each in `fleet/site.yml`. Fleet owns ufw defaults and `alcor_ssh_door` (R98 item 1). No secret in the tree. Check mode must pass. mira gets nothing.
- **redux-queue (593a3cae).** Queue owns the approver on alcor, grant signing and the `admin_ssh` certificate signer. This lane owns the request callers, the node-side `TrustedUserCAKeys` and `AuthorizedPrincipals` config and grant checking with queue's `verify_grant` vendored. Grant kinds: `admin_ssh`, `trust_raise`, `invite`, `browser_signin`.
- **redux-channels (5a3ff6e5).** Channels takes no file in `services/social`. Trust labels are the strings `trusted`, `cooperative`, `untrusted`. A pushed line carries the level as `[level]`.
- **redux-harness (b7312951).** reflection stays Python tonight and keeps the per-session ledger (`trust_get`, hidden `trust_set`). This lane writes the policy as a pure library, `awm.trustpolicy`, with no state. Reflection calls it at start, message, stop, interrupt and the other steering verbs, and at registration. It adds two hooks for R104: an edge check before a `peer=` call, and the hint on a first local message.
- **Edge list schema.** `fleet/edges.yml`: `nodes.<name>` with `role`, `managed`, `addresses` (`lan`, `tailscale`, `public`, `zerotier`), `edges.<from>.<to>: <network>`, `singleton_doors.<host>.port` and `singletons` as a list of `{tool, host}`. Core reserves the `singletons` tool names. Fleet's inventory test compares its hosts with `managed: true`.

Gotchas: a sibling may answer after the work starts. Build each side behind a narrow function, so a late answer moves a call site, not a design.

### T2. Write the edge list as data, with a reader in Python

Write `fleet/edges.yml`: nodes with role (`laptop`, `desktop`, `homebase`, `standby`, `work`, `station`, `backup`, `admin`), the edges from R87, the network of each edge from R96 and the singleton hosts with their door verbs from R89 and R98. Add `awm.config.edges` in `service_components/config`: load the file, `reaches(a, b)`, `reached_by(b)`, `network(a, b)`, `singletons()`. A test checks the table row by row against R87.

Gotchas: the file is the single source for Ansible, Python and Rust, so keep it plain YAML with no anchors. sockeye and fir take keys through the Alliance account, so their inbound edges hold no key lines of ours.

### T3. Delete the foreign and domestic code and replace each check with an edge check

Remove `RELATIONS`, swarm, principal and grants from `awm.config.peerbook` and the gateway's peer book writes. Delete the gateway's foreign gate, the grants context and the foreign parts of `private_gate`, `catalog`, `peer_catalog` and `hub/proxy`. Delete `httpsfront`'s foreign paths. Replace `peer_relation(node) == "domestic"` in `cx`, `reporting` and `social.keeper` with an edge check: the caller's node reaches this node. Drop the foreign branches of journal, search and kb federation. Delete the tests that only test the old system and fix the rest.

Gotchas: the HTTPS transport with node tokens stays until the Rust gateway replaces it, or cross-node calls break in between. Leave `services/board` to redux-channels. `relation` also names a Trilium attribute and a graphify edge, which stay.

### T4. Commit a checkpoint

Commit T2 and T3 together.

### T5. Compact

Handoff: the plan file, the todo list, the agreed interfaces in T1, and the edge list at `fleet/edges.yml`. Next is T6.

### T6. Remove the Slack send as Tony, block the first DM, and bound each seat to its own chats

Delete the send path of `slack_conn`'s session mode and the Slack send in `social/mira/mira_api/drivers.py`. Reads stay. Make `social send` refuse a Slack account that is not the bot, because no function serves it. In `slack_bot_conn`, refuse a DM unless the person has written in that DM, checked against the Slack history in code. Scope `social history` to the calling seat's own chats. Put the seat's level in its pushed line as `[cooperative]` or `[trusted]`.

Gotchas: mira runs the live `social`. Change the repo only. A reply in a thread the person started counts as written.

### T7. Scaffold the agentic-commons workspace and the seat folder initialiser

Write the repo scaffold as a template the main seat installs: `docs/` with the seat SOP moved out of the skill copies, `seats/` gitignored, a DVC config and a README that states the backup rule for lab and outside seats. Add a seat folder initialiser to `social.seats` that makes `seats/<person>/` a local git repository with DVC and no remote. Creating the GitHub repo and the Hallam lab fork goes on Tony's list.

Gotchas: R128 keeps the SOP out of the awm repo. Hold the template in this branch only as the source for the first commit of the new repo.

### T8. Remove the static portal users

Delete `awm auth user-add`, the per-user password store and the daily shared peer password. Write a config task that removes altair's two users. The passkey login (R84, R64) is redux-queue's and must land first in the sweep.

Gotchas: deleting password login before the passkey lands locks Tony out of the portals. Mark the role task to run after the passkey deploy.

### T9. Commit a checkpoint

Commit T6 to T8.

### T10. Make the journal its own service with diff edits and trust-bounded reads

Add `journal edit`: store a unified diff against the post's current body and keep every version. Reads return the latest. `journal history` returns the chain. Bound reads by the caller's trust: trusted reads its node and every node it has an edge to, cooperative its own node, untrusted its own scope. Cooperative and untrusted write only under their own scope. Add an addendum sweep that lists the addendum posts and their targets, with an apply flag for the implementation pass. Write the `journal_user` config in T20.

Gotchas: the journal database is live on every node. Test against a copy in a temp dir. The schema change must migrate old posts as version 1.

### T11. Build the trust policy library

Add `awm.trustpolicy` in `service_components/trustpolicy`: the three levels, `level_for_start`, `may_steer`, `cross_edge` and the R104 item 2 hint text. It holds no state. redux-harness keeps the ledger in reflection and calls the library. Send harness the commit hash.

Gotchas: the level comes only from what started the session. Nothing in a message or header may set it. A raise checks the grant's session id against the session it raises.

### T12. Commit a checkpoint

Commit T10 and T11.

### T13. Compact

Handoff: the plan file, the todo list, and which interfaces in T1 are still pending. Next is T14.

### T14. Build the Rust crates `awm-trust` and `awm-edge` and the service door `awm port`

`awm-trust` mirrors T2 and T11 in Rust and verifies grants. `awm-edge` holds one `ssh -T <node>` stdio channel per edge with `ControlMaster` and `ControlPersist`, frames each request, keeps the link alive, reopens a dropped link and treats a sleeping node as an ordinary failure. `awm port` reads the verb from `SSH_ORIGINAL_COMMAND`, refuses any verb outside the door's set and logs each call. Build and test only in mizar's build container.

Gotchas: wait for core's container command. Test the transport against a local sshd in a container, never against a real node.

### T15. Build the grant request callers and node-side grant checks

Add the callers for each kind: one `admin_ssh` grant per Ansible run covering every node in it, an admin login helper, a knowledge invite and a trust raise. Check each grant with queue's `verify_grant` against alcor's public key.

Gotchas: a request expires 24 hours after it is made. A grant's length runs 15 minutes to 24 hours.

### T16. Build the browser seat singleton

Extend the browser service: one window per attached seat, verbs confined to that window, no raw debug port, one viewer that raises the seat's window, one service window for resident sites, and an own container per seat on request, with Tony's profile only for a trusted session. A captcha or sign-in files a `browser_signin` request.

Gotchas: the live browser on mira and capella's debug port stay as they are until alcor's browser runs (F35).

### T17. Build the Trilium backup pull and diff check

Write the mizar side of R105: pull deneb's nightly consistent copy, compress it with zstd, keep three, and compare with the last approved copy against the thresholds of R105 item 3. Hold the copy on the approvals queue when one trips.

Gotchas: test on a synthetic database. Never open the live vault.

### T18. Commit a checkpoint

Commit T14 to T17.

### T19. Compact

Handoff: the plan file and the todo list. Next is T20.

### T20. Write the node config roles for the fleet pass

fleet.md R140 (2026-11-03) puts node config in the cloud project. Work in scope `cloud/redux-trust` (branch `feat/redux-trust`), which redux-fleet merges. The edge list is `homelab/edges.yml` with its R87 test at `homelab/tests/test_edges.py` (commit 77e7830). Roles go under `homelab/roles/<role>/`. Send fleet the play lines for `homelab/site.yml` instead of editing it. Fleet's `awm_config` role installs the edge list at `<AWM_DIR>/edges.yml`. Fleet owns ufw defaults and alcor's port 22. sshd needs `ExposeAuthInfo yes` for `awm edge-serve`. Each node needs alcor's grant key at `<AWM_DIR>/grant-key.pub`.

Write `ssh_edges`, `users_sudo`, `singleton_door`, `singletons_sandbox`, `rt_user`, `journal_user`, `trilium_deneb` and `wsl_bridge` under `fleet/roles/`, and their `import_role` lines in `fleet/site.yml`. Add shogun's Terraform and lyra's key setup to Tony's list. Run the syntax check and check mode against a local inventory only.

Gotchas: no role touches mira. No secret enters the tree. Check mode must never reach a real host tonight.

### T21. Draft the doc updates in this plan file

List the changes `FEDERATION.md`, `ARCHITECTURE.md` and the redux area docs need, under Callouts. The interview session owns the area docs.

### T22. Commit and report to the coordinator

Commit. Send one line to job 8162a704.

### T23. Debrief

Run the `debrief` skill.

## Callouts

- The HTTPS node-token transport stays in the Python tree until the Rust gateway takes peer calls through `awm-edge`. Deleting it first would cut cross-node calls before the sweep.
- R91 shogun, R94 lyra and R96's Tailscale moves need Tony at the keyboard. They go on the batch list in `docs/redux/drafts/edges.md`.
- The sweep must land these together, or a lane breaks:
  - The Python gateway passes the caller's trust level to services as `_caller_trust` and the caller's own scope as `_caller_scope`, next to `_caller_session`. It reads the level from reflection's ledger, `<AWM_DIR>/services/reflection/trust.json`, where a session with no entry is trusted. The browser treats a missing level as cooperative. The journal treats it as trusted, as the ledger does. Core has finished, so the sweep owns this.
  - The social keeper passes `trust` to `cx start`. harness's `start_other` accepts it from its next commit.
  - The fleet config pass applies the roles in `cloud/redux-trust` and writes one `[[external]]` line per singleton into `config.toml`. The singletons then leave the gateway's spawned services.
  - `rlm-browser` retires when the browser singleton runs: both publish the tool `browser` (gap F38).
- The bare `peer` stamp is now refused everywhere. The legacy bearer still authenticates but reaches nothing. Delete it from `auth` and `httpsfront` once every node runs the edges.
- The journal's goal verbs and `render` carry no trust bound yet. The journal database moves to `/var/lib/awm/journal` when the journal runs as its own user. That move needs a copy of today's `<AWM_DIR>/services/journal.db`.
- altair's two static accounts go when altair's auth runs the 5→6 migration. No node config is needed. The same migration revokes every unscoped device token, so a device that used one needs a new scoped token. `scripts/sirius/add-user.sh` still calls `awm auth user-add` and fails at that step.
- A per-seat browser container needs Docker. The browser singleton runs as a system user, so it needs rootless Docker of its own, not the `docker` group (R99 item 2).

## Doc changes for the sweep

The interview session owns the area docs. These are the changes this lane's work makes due, for whoever lands the docs.

- `FEDERATION.md`: replace the foreign and domestic peer model with the edge list. A node serves a peer only over an edge (`awm.config.edges.admits`). Delete the sections on swarms, principals, read grants and the grant category. Lines now wrong: 72–74, 115, 124–161, 167, 176, 180, 190, 223, 229, 273, 289–291, 303, 317, 319, 406–407, 425. Add the edge transport: `awm edge-serve` over one warm ssh channel per peer, and the service door `awm port` on port 2201.
- `ARCHITECTURE.md`: lines 123, 188–191, 461 and 463 describe the peer book's roles and grants. Replace them with the edge list, `awm.trustpolicy` and the `awm-trust` and `awm-edge` crates.
- `docs/redux/fleet.md` line 201 describes the gap that this lane closed.
- Service INSTALL docs that name domestic peers or grants: cx 48 and 100, httpsfront 123, reporting 37, 204 and 277, kb 7 and 41–49, search 9, 21, 36 and 39.
- The journal's docs gain `journal_edit`, `journal_history` and `journal_addenda`. AGENTS.md's startup ritual gains no step.
- `users.md` R130: the auth service has no user accounts. A device token speaks for one device as `device:<id>` and carries a scope.
- `awm/services/auth/INSTALL.md`: delete § User accounts (lines 51–60). In § Device tokens, lines 64–77 describe per-user tokens. The settings table, line 110, says the lockout counts per username.
- `awm/services/httpsfront/INSTALL.md` line 123 says `/__auth/login` takes a username.
