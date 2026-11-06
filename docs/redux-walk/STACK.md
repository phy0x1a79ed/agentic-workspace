# redux-walk: stack

## Purpose

Take the redux build from its current state to the end state in docs/redux, one decided task at a time, with a top-level worker dispatched for each decided task

Procedure, from dfs-interview § Work the stack:

1. Run `stack.py <folder> current` for the top topic. Ask only its own questions.
2. Dispatch read-only subagents to survey what already exists for the topic.
3. Record each difference between the survey and the user's description.
4. Ask the user about each difference with an unclear answer.
5. Put one question per message to the user, or a small set of closely related questions. Give your recommendation with it.
6. Queue the other questions with `stack.py <folder> ask`.
7. Match each answer to the message it replies to. Ask when a reply could answer more than one message.
8. Record each answer in the topic's `README.md`. Copy the user's words from `quote.py`.
9. Write the result as the current design.
10. Tick each answered question with `stack.py <folder> answer`.
11. Run `stack.py <folder> close` when the topic's question queue is empty.
12. Commit.
13. Compact only when the closed topic ran long, with many exchanges or large reads. After a short topic, present the next topic in the same turn.
14. To compact, run `reflection(verb="compact", args={followup:...})`. Write a followup that invokes `dfs-interview` by name. Name the discussion folder, the closed topic, its commit and the new top topic in it.

## Current topic

**deneb-retry**. Its result: `deneb-retry/README.md`.

## Stack

- **mira-swap** (2026-11-05): Tony (2026-11-05): alcor needs to work in isolation of mira at end state, so alcor needs the relavent keys. expect alcor to take over mira at end state. we are making the swap now. discord and ibkr force us to make the change and 1week test with both running is not possible
- **windows-nodes** (2026-11-05): Tony (2026-11-05): is there a session for setting up the ssh edges? lets fix capella and lyra in one go, including tailscale, etc / they also need the new awm isntall in their wsl instances
- **rt-teach** (2026-11-05): Tony (2026-11-05): we need the rooms service to be up so that the Alcor agent can teach the RT agent, which was the original session that created the RT stock stuff, how to actually use the new system and edit.
  - [x] Which machine and session was the original RT agent? rt-container found no trace on mira or mizar. Leads point to your Mac, or capella or lyra over ssh. (answered 2026-11-05)
  - **rooms** (2026-11-05): from redux-build/dev: dev (2026-11-05): the RC does not support the rooms check: the door carries no caller session, members receive only by an outbound push from deneb (R79), isolated sessions cannot reach a door singleton, a room has no second member, and remote holders are never pruned. About a day of build.
    - [x] Does the door-client window wait for rooms? Recommended: no. Run the window when the RC is green; rooms gate the mira-to-alcor rt move, where the alcor agent teaches the rt agent. (answered 2026-11-05)
- **board-intake-invert** (2026-11-05): from redux-build/dev: dev (2026-11-05): board intake (R77) needs operations on alcor, so with the board host on deneb intake never delivers; events queue in deneb's board log. Inverted shape: alcor's operations pulls from deneb's board door. R77's door and intake are slated for deletion (T18 feat/redux-r77).
- **zotero-order-invert** (2026-11-05): from redux-build/dev: dev (2026-11-05): with R79, zotero's Sync now (R83) never runs on deneb, because deneb's zotero long-polls alcor's approver (order_pull), which is outbound. Invert it: alcor's approver calls a zotero verb on deneb's door with the signed envelope.
- **deneb-retry** (2026-11-05): from redux-build/deneb-board: deneb-board (2026-11-05): the cutover failed after the vault move. f5c5294d refuses deneb's plugins because they need operations/social, which only door-client lets a node meet through a singleton's door. Rolled back; 2b (sshd/ufw/SNI) kept. The retry needs a release with door-client.
- **mizar-backup** (2026-11-05): from redux-build/rt-container: rt-container (2026-11-05): mizar had no dvc binary; data_dvc's fallbacks look under ~/lib/miniforge3 but mizar's miniforge is ~/miniforge3, so scope create/heal report mode none. Both nightly chinook jobs on mizar (cache_sync, workspace_backup) fail: .awm/dvc.toml [chinook] is missing, so nothing on mizar is backed up off-site.
- **dispatch-mode** (2026-11-05): as tasks are decided and worked out, please dispatch top level agents to work on them / their issues can be pushed back to the stack / we will notify them once their issues are solved and how, rather than when we decide how to solve. / we can add this mode to dfs-interview skill
  - **parallel-uptime** (2026-11-05): I'd like to be able to work in parallel. if all discussion goes through you, then I sit idle while you work and vice versa. our first topic should be a protocol taht maximizes both of our up times
    - **board** (2026-11-05): the "One file holds everything" is not a hard blocker, can we look into setting up trillium and the new board then? this would include the alcor services
      - [x] Does alcor open for the board's services only (operations, approver and passkey, deneb tunnel), with rt, IBKR and mira still shut? (answered 2026-11-05)
      - [x] Tony's DNS steps M10, M12, M13, M14: can he do them now, in parallel? (answered 2026-11-05)
      - [x] Discord on alcor: hold social's Discord accounts on alcor until mira's cutover? (answered 2026-11-05)
      - **door-client** (2026-11-05): from redux-build/dev: alcor-open, needs your decision: 20 plugins are refused on alcor. The root is trilium. ... The gateway has no client for a peer's singleton door, so it lists peer tools only from edges.
        - [ ] Tell alcor-open to resume step 6 (approver check) and any role held for operations when the door-client release lands
        - [ ] Deploy door-client only after deneb's door answers with service keys (keys role from deneb-board; dev applies on mizar, alcor-open on alcor)
        - [ ] After door-client is live: remove mizar's ZOTERO_SYNC_ENABLED=0 drop-in and zotero from every node but deneb
      - **alcor-door** (2026-11-05): from redux-build/alcor-open: alcor_door: FIREWALL on port 22. It allows 22 from mizar 100.125.95.5, capella 100.90.179.70 and lyra 100.85.115.29, then deletes the blanket 22 rules ... After it, port 22 on alcor answers only those three tailnet addresses. No LAN ssh, no phone.
      - **deneb-board** (2026-11-05): trilium-fork follow-ups: deneb gets the tarball only once cloud's install config makes deneb a push node; cloud deploy.yml awm_fleet_push.altair still lists trilium and canonical_repos defaults include trilium; the mizar vault mirror does not exist yet
        - [x] Why does deneb need nodejs, npm and build-essential if mizar pushes built artifacts? (asked deneb-board) (answered 2026-11-05)
        - [x] When core-artifact lands: tell deneb-board the artifact names and route (cloud tip 88c35a8 holds) (answered 2026-11-05)
- **path-front-door** (2026-11-05): Tony raised a path-based front door (nexus.tony-xy-liu.com/<node>/...) as a possible later design; not decided. (via dns-guide, 2026-11-05)

## Parked

From journal 6a98c0c6 (awm/redux) and walkthrough's handover (manual-steps.md at feat/redux-walkthrough a12a5f6e, M-numbers). Live workers: dev, singleton-callers, rt-container, reflect-selfstop.

- **operator-restart** (2026-11-05): from redux-build/tether-redial: mizar's tether operator ends every live session when it stops, so each awm restart or deploy on a member drops its tethers and needs a fresh invite. Sessions should resume across an operator restart.

- **altair-pass-2** (2026-11-05): leftovers of redux-build/dev (journal 8d757654): (1) the three old env.bak copies in altair ~/handoff/altair-rc-backup-20261105-1928/old-env-baks may hold the issuing token, which R147 keeps on deneb only: delete them with the rollback env once altair has run clean; (2) search's v2 database is set aside as search.db.pre-rc-v2 with no migration, so check that altair's search reindexed.
- **users-sudo-admin-group** (2026-11-05): from redux-build/shogun: cloud-init on shogun failed to create "admin" because Ubuntu ships an admin group (fixed in arbutus-infra ec10e1e). Check that the cloud repo's users_sudo role (ansible.builtin.user "admin") handles that group on every Ubuntu node.
- **rt-data-check** (2026-11-06): from redux-build/rt-ready: live versus delayed data cannot be confirmed off-hours. On Friday after 06:30 PT, check the rt log for "[TSLA] data lag Ns -> LIVE" or the #rt-signals feed. Market data streamed on Tony's live username at the same time can make IBKR withhold data from the paper session (error 10197). frames_*.pkl (DVC) is missing on alcor; only the disabled bt-compare uses it.
- **mira-leftovers** (2026-11-06): from redux-build/mira-swap (journal bd169330, retired): moved and live on alcor: 2fa, social (16 seat holders), IBKR paper, rt. Disabled on mira. The ssh slot arbiter runs per node. Left before mira's decommission: (1) HPC keys for alcor's ssh arbiter, through shogun; (2) browser and Chrome logins for the parked Teams/Slack automations and onedrive-ubc; (3) the Opera-backed social accounts wait on (2); (4) a home for guacamole, zerotier and gnome-remote-desktop; (5) mira's generic awm services and data, then the disk move into mizar; (6) PEER-key removal on capella and lyra; (7) the frames DVC remote (bt-compare only). Tips: cloud dev 9cb7ad8 on mizar, stock 772b7c8.
- **rt-teach-proposals** (2026-11-06): from redux-build/rt-teach (journal 1f58afb9): P2 a preview mode for `rt pin`; P3 rule: no AUTOSCAN, bt-compare or scan-ab without a client-side Massive throttle; P5 drop the dead WATCHLIST_FILE env or read it in ibkr_day.py; E1 the same-day watchlist guard (tested, rt-agent can land it under Tony's grant); P6 rooms: keep the relay backlog across plugin reloads, name the content rule in channel.write's 403, a guest leave that keeps the room, a hand-off verb, attachments, a how-to fix (curl, urllib 403, always use next). P4 (watchdog alert) went to ibkr-native. Also: run_day.py.orig:33 holds an 8-character api_key literal from mira's snapshot; rotate at Massive if it was ever real.
- **rc-archive-verb** (2026-11-06): Swarm triage archived the 31 dead Remote Control rows through POST /v1/code/sessions/<id>/archive with the CLI's stored login (GET /v1/code/sessions?limit=100 lists them, status active/archived). Script: job 8162a704 tmp/rcrows.py. Make it a reflection verb (archive a row by name, refuse a live job), and have `stop`/`stop_other` archive the row when they remove a job, so dead rows stop piling up.
- **rtmac-alcor** (2026-11-06): from redux-build/rt-mac: rtmac (100.74.29.49, shown as macbook-air-3) is live on mizar. cloud dev 0c9f367 holds its edges.yml entry (rtmac to mizar and alcor, callers of ibkr, massive, relay, zotero). After Fri 13:00 PT, run `--tags ssh_edges` and then `--tags alcor_door`, each with `--limit alcor`, check first, holding a second login. Then she runs `ssh alcor true`. UPDATE 2026-11-06: redux-build/rt-room moved her off ZeroTier. Cloud dev 7edaf83 adds rtmac edges to capella, lyra, altair and mira over Tailscale. Her key is on capella (ssh_edges) and on lyra (authorized_keys line only; the full ssh_edges pass waits for lyra's rollout). She checked all four over Tailscale and uninstalled ZeroTier. The tailnet policy is rendered but not applied: Tony pastes it, after checking that it lists his phones. Tony decides: the live tailnet policy is permissive (her Mac reaches altair and mira with keys she already had). Pasting the rendered tailnet.yml policy cuts those routes, and any device that edges.yml does not list, such as phones.
- **deneb-edge-502** (2026-11-06): from deploy-fix/capella-daemon: deneb runs 0f19ebc6 (pushed by hand to /opt/awm.git, artifacts rsynced, deneb-station/refresh), but every peer=deneb call from mizar returns 502 "the link dropped", before and after deneb's restart. mizar runs no ssh edge-serve process to deneb.
- **shogun-pass-2** (2026-11-06): from redux-build/shogun (journal 523a8e22): shogun 6bd155dc runs awm deb4d72d (33 plugins, 0 refused); cloud dev d7098ec on mizar, arbutus-infra feat/tony ec10e1e. Left: admin edges from shogun (workers and bastion need Tony); the admin-key move with the real path dev/secrets/txyliu.pem (arbutus_infra_move defaults are wrong); the mizar backup and the arbutus-infra move; remove the client gates and admin keys on capella, lyra, mizar, altair (Tony edits, M29); arb_announce through alcor's social; shogun_node and M37/M38; Ryan's keys M6/M40 (wait on Ryan); users_sudo and Ubuntu's admin group; manual-steps Phase 5 is stale (envs/shogun, flavor, lock sign). Cloud's origin is altair's copy with dev checked out, so mizar's dev is the landing.
- **singleton-unit-restart** (2026-11-05): from redux-build/session-bound-door: the deploy route never restarts the system singleton units (awm-plugin@operations, 2fa, browser, social), so their changes miss alcor. On alcor operations still runs the front from before 0f19ebc6, and its cards carry no session (card mUSGWWjD). After Friday 13:00 PT, restart awm-plugin@operations on alcor, then re-check that a mizar request_human card carries the session. Then add the singleton restart to the deploy route, and add operations to run-tests.sh (no deploy check runs its tests today). session-bound-door journal dc8975e1.
- **rt-telegram-token-cmdline** (2026-11-05): from redux-build/session-bound-door: the rt bwrap sandbox on alcor takes the Telegram bot token through --setenv, so any alcor user can read it in /proc cmdline. Move it to a file or a systemd credential, then Tony rotates the token. Do this after Friday's close.
- **ibkr-native** (2026-11-05): from redux-build/mira-swap: IB Gateway hangs after authenticating inside alcor's rootless Docker container (slirp4netns), for paper and live alike. Tonight it runs natively as awm-ibkr in the transient units ibkr-native-test and ibkr-native-sock (/run/awm-ibkr/ibapi.sock to 4002), with drop-in zz-native-gateway.conf (IBKR_AUTOSTART=0) on awm-plugin@ibkr, stock 772b7c8. The units do not survive a reboot, and `ibkr start`/`restart` would start a second login. Make the native Gateway a real unit in cloud, or fix the container network (pasta or rootful), then delete the drop-in and the transient units. IN PROGRESS: redux-build/ibkr-native (cb558b53), also paper-only (Tony 2026-11-06: "Please ensure that the IBKR service uses exclusively the paper account."), ReadOnlyApi=yes, 4002 on loopback, socket not world-writable, watchdog alert. Applies after Fri 13:00 PT. Until then nothing restarts the Gateway if it dies (rt-ready, 2026-11-06).

- **windows-pass-2** (2026-11-05): leftovers of redux-build/windows-nodes (journal eb625841, cloud dev 3d406e0): (1) lyra: elevated run of awm-wsl-bridge.ps1 over a v0.4.1 tether with Tony, then ssh_edges, its key into edge_keys.yml, mizar and capella, its altair line, and the awm install; (2) lyra aw: drop the aw-migrate key, export aw to A:\linux\aw-backup.tar, unregister with Tony's OK (8.7 GB copied to ubuntu ~/from-aw); (3) capella: elevated awm-wsl-bridge.ps1 for the 10022 forward; (4) tailnet_portal for capella and lyra through Windows' tailscale.exe; (5) capella's avatar plugin to the new manifest; (6) capella: close the 16122 forward with its Any rule, and Chrome port 19222; (7) capella→altair works (2026-11-05): a hand block `Host altair / ProxyJump none` sits above the awm-edges Include in capella's Windows config, and altair's ed25519 host key is pinned in its known_hosts. Make the ssh_edges render emit `ProxyJump none` for each edge host, then drop the hand block. Do the same on lyra; (8) lyra refuses ssh from mizar (host key verification failed; tailnet name now lyra 100.85.115.29, tailnet-names 2078f7a6): re-pin lyra's host key, set its OS hostname.

- **deneb-pass-2** (2026-11-05): leftovers of redux-build/deneb-board (journal f4ed07ad): (1) move the relay seats and keep's claude daemon off deneb (R79, R129), then set deneb_agents_moved: true; (2) capella's avatar forward uses the socket (M44); (3) lyra's edge key on deneb; (4) derived relay callers (`members`) with dev.

- **tether-dial-timeout** (2026-11-05): from redux-build/tether-redial (journal 8947612e; join-403 solved there: the edge bridge turned upstream 404 into 403, fixed in awm bd764a6f, tether v0.4.2). lyra's slot-343 owner redialled for 10 min and no dial reached deneb. Add a bounded per-dial timeout to the owner client if it repeats.

- **edge-forward** (2026-11-05): Tony: "ssh needs to be just plain ssh". Replace `awm edge-serve` on the far node with a plain ssh forward to its loopback gateway (R92: an awm request inside ssh to the other node's loopback gateway). Delete edge-serve and its AWM_DIR lookup. dev, after R147.

- **alcor-pass-2** (2026-11-05): leftovers of redux-build/alcor-open (journal 73dd9ef5): (1) lyra's edge key into alcor's ssh_edges once recorded; (2) operations gets AWM_APPROVER_GRANT_PUBKEY through singleton_needs or secrets, not config.toml [env], so spawn orders turn on; (3) singletons_sandbox stops a singleton that leaves the list; (4) deneb out of alcor's singleton callers (with deneb-board); (5) tailnet_portal on mizar, capella and lyra.

- **teams-slack-automations** (2026-11-05): Tony: "I'm deferring the non-essential browser automations like teams and slack, running on opera on mira". They stay on mira for now. social's slack "me" and teams accounts are off on alcor until they move.

- lyra: run awm-wsl-bridge-lyra.ps1 on xps-laptop (M18), ssh 100.85.115.29 lands in WSL, then start WSL (M41)
- tailnet: altair joins (M21), Tailnet Lock (M22), ACL from tailnet.yml (M36)
- shogun: Ryan's keys (M6, M40), auth key and lock-sign (M35), app and wake credentials (M37, M38), its key into altair (M39); shogun stays planned
- goal.md and the ~/.claude drafts: confirm the CLAUDE.md standing frame (M4, goal 50708f67), then install
- agentic-commons: private repo on phy0x1a79ed plus a Hallam lab fork (M8)
- cloud repo: canonical home on mizar and a GitHub backup; cloud dev is 58339c4 on mizar, local only, guarded container run green (dev, 2026-11-05)
- build-node odds: qemu binfmt (M2), push approvals (M23), /mcp reconnect on deployed nodes (M24), cloud env network level (M53), bookmarks to /p/<plugin>/ (M54)
- avatar stage client port (M11), needs capella awake
- DNS and portal: auth A record, CAA, DNS token and ACME email (M12-M14), cutover check channel (M7), portal hosts (M10)
- alcor sitting: deploy, real user CA, Chrome logins, Duo, passkey, Ansible apply (M19, M25-M29, M33, M42-M52)
- mira: rt-container's design (feat/rt-container abbe1ac holds the stock-market and rt callers; merge only with the stock plugins; the plugin build waits on Tony's go to rt-container), IBKR (M30), cutover play (M31), capella debug port (M32), held until Tony opens mira and alcor
- Discord seats (M34)
- R77 after the Caviar and muse invites (M55)
- reflection follow-ups from reflect-selfstop (release dcd8ba4d, journal 8542e517): cch status misses a session by its post-clear id, so stop {job:<new id>} misses it; old trust.json records have no job field
- altair still commits nightly vault snapshots to its fork release; R105 stops that (trilium-fork, 2026-11-05)
- search-awm-api follow-ups (journal e06acc4b): `search sources` counts awm docs for singletons a relayed caller cannot see; a stale `_grants` line in search INSTALL.md
- spawn-tuning follow-ups (journal 0fc35624): cch test_stop_refuses_a_caller_whose_mode_is_unknown fails on 9d346fb4 too; run-tests.sh does not run the operations plugin tests
- core-artifact follow-up (journal 88a132af): pages of an out-of-tree plugin (AWM_PLUGIN_ROOTS, such as the optional plugin projects of R33) are not in pages.pin, so no push node gets them
- homelab_base "Fetch Docker's apt key" reports a change on every run, so the role is not idempotent (alcor-open, 2026-11-05)
- end state: verify every node (T28), retire mira and its disk (T29, M56), debrief (T30)
- mizar off-site data backup: chinook is ruled out (lab resource) and mizar's chinook jobs are disabled; pick another target later (Tony, 2026-11-05)

## Workers

- **redux-build/trilium-fork (job e74cd7f2): the fork on mizar, its first release tarball, server.pin** (2026-11-05): took board
- [x] tell redux-build/trilium-fork how tarball-host was solved (told 2026-11-05)
- **redux-build/alcor-open (job 839aad63): alcor onto dcd8ba4d and its roles, dry run first, rt, IBKR, mira and Discord held** (2026-11-05): took board
- **redux-build/dns-guide (job 67cbdc6d, sonnet medium): guides Tony through M10, M12-M14** (2026-11-05): took board
- **note: users_sudo on alcor held as Tony's M29 (ssh admin gate); social on alcor runs with no accounts** (2026-11-05): took dispatch-mode
- **redux-build/deneb-board (job a4d345a8): deneb push node, trilium leftovers, deneb dry run; apply waits on Q-deneb** (2026-11-05): took deneb-board
- **redux-build/search-awm-api (job 8790d479, opus high)** (2026-11-05): took search-awm-api
- **redux-build/spawn-tuning (job 716a0319, sonnet high)** (2026-11-05): took spawn-tuning
- [x] tell redux-build/deneb-board how trilium-shape was solved (told 2026-11-05)
- **redux-build/core-artifact (job 96e615ee, sonnet high): pinned musl core binary for push nodes (from dev's deneb route answers)** (2026-11-05): took deneb-board
- **redux-build/door-client (job 48198ebe, opus high): gateway client for a peer's singleton door; needs check counts door-routed tools as declared** (2026-11-05): took door-client
- **redux-build/deneb-singletons (job 8e4c71f0, opus high): trilium refuses an empty vault, zotero through the trilium tool, per-singleton secrets and tether paths** (2026-11-05): took deneb-gaps
- **redux-build/pin-refresh (job b51aabfa, sonnet high): routine deploy rebuilds stale core and page pins** (2026-11-05): took pin-refresh
- [x] tell redux-build/dev how zotero-home was solved (told 2026-11-05)
- [x] tell redux-build/deneb-board how deneb-gaps was solved (told 2026-11-05)
- [x] tell redux-build/dev how pin-refresh was solved (told 2026-11-05)
- **redux-build/session-cleanup (job 79aa5482, sonnet medium): inventory and cleanup only; the listing design is for discussion** (2026-11-05): took session-list
- **redux-build/dev** (2026-11-05): took mizar-backup
- **redux-build/deneb-board** (2026-11-05): took tarball-backup
- **redux-build/rooms** (2026-11-05): took rooms
- **redux-build/windows-nodes** (2026-11-05): took windows-nodes
- **redux-build/tether-issue** (2026-11-05): took tether-issue-door
- [ ] tell redux-build/dev how tether-issue-door was solved
- **redux-build/mira-swap (job 99a8ed28, opus high)** (2026-11-05): took mira-swap

## Closed

- **tarball-host** (closed 2026-11-05): `dispatch-mode/parallel-uptime/board/tarball-host/README.md`
- **search-awm-api** (closed 2026-11-05): `search-awm-api/README.md`
- **spawn-tuning** (closed 2026-11-05): `spawn-tuning/README.md`
- **trilium-shape** (closed 2026-11-05): `dispatch-mode/parallel-uptime/board/deneb-board/trilium-shape/README.md`
- **zotero-home** (closed 2026-11-05): `dispatch-mode/parallel-uptime/board/deneb-board/zotero-home/README.md`
- **deneb-gaps** (closed 2026-11-05): `dispatch-mode/parallel-uptime/board/deneb-board/deneb-gaps/README.md`
- **pin-refresh** (closed 2026-11-05): `dispatch-mode/parallel-uptime/board/deneb-board/pin-refresh/README.md`
- **session-list** (closed 2026-11-05): `session-list/README.md`
- **tarball-backup** (closed 2026-11-05): `dispatch-mode/parallel-uptime/board/tarball-backup/README.md`
- **tether-issue-door** (closed 2026-11-05): `tether-issue-door/README.md`
