# session-cleanup: fewer sessions to revive on each restart

> I also want to clean up the current list of sessions to prevent churn from restarts

A pinned job is revived at boot (`wake boot`), and each awm restart reconnects every live session. So every idle session left running costs a revival and a reconnect.

## Sessions on 2026-11-06 at about 18:30Z

Proposed **stop** (work done, nothing waits on them):
- mizar 76fc7fe0 awm-dev (pinned): "awaiting retire decision". Its altair and seats fix is landed (journal ab9b81af).
- altair 3bd18b39 refresh-awm-0f19ebc6: a deploy supervisor that the deploy reused for 5aff3f26. It is done. The next deploy starts a fresh one.
- altair a0784d1e msm 0.25.1 quota check (pinned): "worktree clean".
- altair c0fc8c2b `<warm egret>`, done.
- altair 66 harness-ds rows with no job: DeepSeek-harness records, mostly deepseek-harness/dev, svc-deepseekh, svc-hermes and kanban. The harness is down ("connection refused" on every node). These are records only, and they need a cleanup verb.

Proposed **ask the session's owner first**:
- altair cb02f98b engcyc data: it asks "close, or keep it open as the relay?"
- altair 1f4fb0a9 `<warm vole>` (blocked) and 49b71d95 `<warm tanager>`: pool spares. altair has three where its pool wants one.
- deneb's five relay seats (30bbc7a3, 512feb43, 997210b5, 9a6fc8c0, dcec04d6), done. Journal f4ed07ad moves them to mizar.

**Keep** (live, or waiting on Tony):
- mizar: steward c20d4fcd, expedition 0e4fe64d (needs your name picks), and this session's workers.
- altair seats: arbutus, Hallam general, the DMs (Beth, Rokaiya, Steven, Rajan, Kalen) and slack-main. seat-lifecycle changes how they retire.
- altair waiting on Tony: engcyc portal c01ec421, msm-writing 9c8233d9, engcyc-raw-data 15a510ca (card GHw1A48a).
- altair macos bu 747aac5c (working).
- alcor, capella and shogun: no sessions.

## Tony's answer and the result (2026-11-06)

> just stop all sessions for now that have no pending work (as in can not continue and are not working, even if I go and answer their questions)

> even for seats of lab messages and DMs, they should retire and not just sit there. like Rajan was barely used, same with Kalen, at least recently.

Stopped and removed, about 18:45Z:
- mizar: 76fc7fe0 awm-dev.
- altair: 3bd18b39 refresh-awm-0f19ebc6, a0784d1e msm quota check, c0fc8c2b and 1f4fb0a9 (warm spares), cb02f98b engcyc data, and the idle seats 0452781d Rokaiya, ec8d0317 Rajan, 7c76de92 Kalen, 8d58ad73 arbutus and 6d606819 Hallam general.
- deneb (with `claude stop` and `claude rm` as the awm user, because deneb has no reflection plugin): 30bbc7a3 unimatrix0 bots, 512feb43 and dcec04d6 (two Discord DM seats for Tony), 997210b5 fxdiff-design and 9a6fc8c0 spawn-test-deneb.

Kept:
- slack-main 346ce8e4: it holds the Slack seats and starts a per-person agent when a message arrives.
- Beth 7e0091e4: it is working, watching for fir to return.
- Steven 31bd528d, engcyc portal, msm-writing, engcyc-raw-data and expedition: each waits on Tony's answer.
- macos bu 747aac5c: working.
- steward c20d4fcd, one altair pool spare, the install-loop pair (holding until Saturday) and this session's workers.

Not stopped: altair's 66 DeepSeek-harness rows. They have no job, and only the harness, which is down, can clear them.
