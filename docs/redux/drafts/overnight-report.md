# Overnight build report, 2026-11-03

## Purpose & Contents

This file is the morning report of the first redux build night: where each lane's work lives, what waits on Tony, and what the sweep must do. `overnight-lanes.md` holds each lane's status line. `overnight-questions.md` holds every question with its options and the sweep notes in full.

## Where the work lives

Every commit is local. Nothing was pushed, merged into dev or release, deployed or applied to a host.

| Lane | Branch and head | Built |
|---|---|---|
| core | awm feat/redux-core 65907c3d | build container, Rust core (gateway, loader, MCP proxy, CLI, bootstrap), usage per plugin, caller trust and scope, the isolated-session door. Contract v1.6 |
| harness | awm feat/redux-harness c0276daa | compaction fix (thread 7), cch and dsh, services off claudedaemon, reflection wait, resumable retire, isolated sessions |
| plugins | awm feat/redux-plugins 3db13252 | R134 removals, mic absorbs virtmic, manifests for 23 folders, the usage emitters |
| channels | awm feat/redux-channels bbc0b926, tether feat/redux-channels a046742 | tether returns the shell (R138), relay rooms, the channel tool |
| sessions | awm feat/redux-sessions 9ca2ce61 | task ids, scope claims, delete guard, R9 session stamps, keeper online gate |
| routines | awm feat/redux-routines 1df74bfc | routine plugin, deploy engine (dry run 16/16), metasmith release routine draft |
| queue | awm feat/redux-queue 0fe170f5 | task store, relay, approver with passkeys, batches and windows, panel with 2fa strip, Trilium board |
| trust | awm feat/redux-trust 5ae958f1, cloud feat/redux-trust a547baa | edges replace foreign/domestic, grants, trust policy, browser seats, Slack send-as-Tony removed |
| fleet | awm feat/redux-fleet 295129f8, cloud feat/redux-fleet 2225de3 | wake plugin, Zotero Sync now, the R108 Ansible pass under homelab/ |

search-finish on altair finished its code phase (T1 to T7) on kb feat/shards and awm feat/svc-kb.

## Waiting on Tony

1. Scope delete guards (R44): two readings, each built one way. See question 6.
2. Drop the retired `awm_penpot` table that holds plaintext passwords? See question 7.
3. How Caviar's stream and the muse link work once the board door goes. See question 8.
4. The room link path, `/tether/r/` or `/r/`. See question 9.
5. Fleet values only Tony holds, and the approver's origin name. See question 10.
6. search-finish: whether the prior lifts hits or only breaks ties, and the recall@10 parity gate. See card 0yMagpehpU1Q on altair.
7. The thread 7 requirement, now that the fix exists.
8. Confirm `goal.md`.
9. Deploy the compaction fix ahead of the sweep, or wait.

## The sweep

Merge the lanes in one pass. `overnight-questions.md` § sweep notes lists each known conflict and pairing. The main ones:

- Merge feat/redux-core with feat/redux-plugins, because the docs link to the contract.
- Re-apply the sessions lane's cx edits on cch's new paths.
- Add `scope` to four Caller literals.
- Switch reflection's and operations' own-session verbs to `_caller_session_id`.

Then apply the host changes that the queue, trust and fleet plans list. Edit the global `CLAUDE.md` and the workspace `AGENTS.md`, which still point at graphify.

## Load

The monitor sampled mizar and alcor every 30 s from 09:21Z, and still runs (`/home/tony/.claude/jobs/8162a704/tmp/loadmon/`). mizar averaged load 4.3 and peaked at 13.9 on 4 cores. CPU pressure peaked at 81%, the package peaked at 67 °C, and it never throttled. Free RAM never fell below 8.9 GB. alcor stayed idle the whole night.
