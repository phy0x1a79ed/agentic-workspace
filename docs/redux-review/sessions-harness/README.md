# sessions-harness

## Purpose & Contents

This doc holds the review of the session system and the harness plugins: how they are built today, where the build and `docs/redux/sessions.md` and `install.md` R135 disagree, and each decision Tony makes on them. The settled decisions go into `sessions.md` and `install.md` as requirements.

## As built

Surveyed read only on mizar, 2026-11-03. "release" is `origin/release` at 266f4b29. The local `release` ref is stale at b664e80f.

- **Deployed.** Nodes run cx, keep and reflection. No cch exists. dsh acts on the harness, never on one session.
  - R141 is live (5453644f). reflection resends on a worker swap (`reflection/inject.py:793-822`) and raises an alarm on a give-up (`alarm.py:67`).
  - R78 is part live. The witness fix (bb028145) shipped. No internet check exists.
  - The other `sessions.md` gap rows still hold.
- **`feat/redux-sessions`, 12 commits.** Merges cleanly with release.
  - Task ids (R58, R57): 86c67200 mints the 8-character id (`reporting/task_ids.py:19-25`) and adds `operations task_*` verbs (`task_verbs.py:98-192`).
  - Journals (R14, R15, R17): 47e3714b stamps the R9 id, accepts a named session, checks `cites` and records each `journal fetch` as a read.
  - Starts (R18, R56): b3a75403 adds a lobby, a `suggest` flag, `task_id` and `role`, and the naming rule in the start brief (`cx/lifecycle.py`).
  - Lineage (R13, R16): b09903d2 records the parent on start and on clear, with an `operations lineage` verb.
  - R78: 6709c060 adds the internet check and a `local_only` mode, off by default.
  - Drafts of the retire, parallel-work and seat-holder skills. No test result is recorded.
- **`feat/redux-harness`, 8 commits.**
  - R135: ced70632 moves cx and keep into `services/cch`. reflection calls the providers through `harness.py`. dsh serves the contract as `harness-ds`. The OpenCode modules are gone.
  - Callers: 0478489f has services ask reflection for the calling session (`gatewayclient/sessions.py:11`).
  - R9: `reflection/ids.py`. whoami and `start_other` return R9 ids.
  - R10, R51: stop keeps the job by default, and `remove` deletes it (`cch/hub_adapter.py:452-455`). `reflection wait` and its sweep exist (`hub_adapter.py:359-386`, `waits.py:81-123`).
  - Isolated sessions run in rootless Docker (b6394431).
  - R141 is redone as a different patch (38a8c1b1), then moved into cch.
  - Tests at ced70632: cch 695, reflection 52, dsh 38 and gateway 1124 pass. 9 social Slack tests fail on a missing `slack_sdk`.

## Differences from the design

- **R9.** Three id parsers exist (`reflection/ids.py`, `journal/sessions.py:26`, `reporting/task_ids.py:32`). The last two need 16 or more hex digits. R9 allows any length.
- **R10.** The design says the job is gone after a retire. The harness lane keeps it.
- **R14, R17.** The lanes disagree. The harness lane still writes the raw Claude UUID and drops a named session. The sessions lane writes the R9 id and accepts one.
- **R15.** Only `journal fetch` counts as a read. `search get` and transcript reads do not.
- **R51.** The wake fires when every listed session is stopped, not finished (`waits.py:96-99`). A session that itself waits counts as stopped, so a nested wait wakes its parent early. A terminal session cannot wait (`hub_adapter.py:376`).
- **R57.** whoami sends `session` to `reporting task_for_session`, which reads `session_id`, so whoami's task id is always null (`reflection/hub_adapter.py:142`). cch declares `task_id`, `role` and `suggest` and never reads them. A join tells the main session only when it is a background job (`task_verbs.py:112`).
- **R8, R12.** Retire is still a skill. Nothing builds R12 item 4.
- **R78.** `local_only` is off by default, so one keeper still checks every box.
- **R67, R68.** Only the seat-holder draft exists. R68 waits on `queue.md` R65.
- **R135.**
  - The contract is keyed by UUID (`harness.py:22`). R117 item 2 now names it `harness@1`.
  - Under the Python gateway reflection falls back to a hard-coded provider list (`harness.py:26-30`). R117 item 4 says a dependent never names a provider.
  - The gateway, reporting and transcripts still import `awm.claudedaemon`, and cch depends on it as a shared library (item 6).
  - reflection still serves cx and keep verbs: `daemon_*`, `seed_other`, `claim_other`, `remove_other` (item 4).
  - dsh cannot name the calling session, so reflection refuses a dsh session that acts on itself (item 3).
- **R143.** Not built. Neither the cch start nor the `reflection cd` followup carries the brief. R135 item 3's contract names no start hook.
- **The two lanes conflict.** A dry merge of sessions into harness conflicts in `cch/cx_adapter.py`, `posts.py` and two tests. Three sessions-lane files land under the renamed cx. Harness against release conflicts in the R141 files.
- **Lane choices not in the docs.**
  - Harness: a `harness_event` done and lost callback, a hidden `reflection session_of` verb that costs two gateway hops per journal post, the old state paths under `services/reflection/pending` and `services/keep`, an access-token login for isolated sessions (proposed as `fleet.md` R109 item 7), and reporting lines through `reflection message`.
  - Sessions: the lobby at `~/.local/state/awm/lobby`, and a waiting session that keeps its scope claim. redux-queue mints task ids in a third copy (`queue-relay/ids.py:12`).
- **Contradictions in the docs.**
  - `drafts/harness-contract.md` keys the contract by UUID, against R117 item 2.
  - R135 item 2 puts "who is calling" in reflection, and item 3 puts "which session is calling" in the harness.
  - `AGENTS.md` still says OpenCode loads it through `mcp-opencode.json`, against R135 item 5.
  - The R18 and R51 gap rows still describe cx. `sessions.md` has no row for R141.

## Decisions

- 2026-11-03: A retire deletes the job. A wait keeps it, and the wake resumes it. A resume of a retired session starts a new job from its transcript, with the same session id (`sessions.md` R10, R51). Tony: "Yes".
- 2026-11-03: A listed session counts as finished only when it retires, so a nested wait holds. A terminal session may wait (`sessions.md` R51). Tony: "Agree on both".
- 2026-11-03: The retire triggers the wake from `reflection stop`. The sweep goes (`sessions.md` R51). Tony: "we dont want sweep, we want active triggers from reflection stop".
- 2026-11-03: Retire states that its last step is `reflection stop` (`sessions.md` R8). Tony: "retire then explicitly state that reflection stop must be called. a session that just ends and sits would be detrimental."
- 2026-11-03: A worker on another node wakes its waiter through that node's reflection. A failed wake raises an alarm and keeps the wait. reflection reads the waits once at start and raises an alarm for a wait on a lost session, which is recovered by hand. A wait clears when its waiter wakes, is resumed or retires (`sessions.md` R51). Tony: "otherwise agree".
- 2026-11-03: A citation candidate is a `journal fetch` or a `search get` of a journal post. Transcript reads, the injected brief and `brief_read` are not (`sessions.md` R15). Tony: "makes sense. but since context are jurnals and are injeted, they  should effectively stay out of this".

## Follow-ups for the build

- Land the harness lane first. Rebase it on release's R141 fix (5453644f) and drop its own patch (38a8c1b1). Then merge the sessions lane into it, moving its `cx/` files under `cch/`.
- Use one R9 parser, `reflection/ids.py`, in journal, reporting and queue-relay. Accept ids of any length (R9).
- Make the harness lane's journal write the R9 id and accept a named session (R14, R17).
- Record a `search get` of a journal post as a read (R15).
- Rework `reflection wait` (R10, R51):
  - Make retire delete the job, and keep the job only for a wait.
  - Count a listed session as finished only when it retires.
  - Let a terminal session wait.
  - Wake from `reflection stop`, across nodes, and drop the sweep.
  - Alarm on a failed wake and once at start for a wait on a lost session.
  - Clear a wait when its waiter wakes, is resumed or retires.
- Make the retire skill end with `reflection stop` and say why (R8). Land the retire draft.
- Fix whoami's call to `reporting task_for_session`, which sends `session` where the verb reads `session_id` (R57 step 2). Make cch read `task_id`, `role` and `suggest` (R57 step 3, R18). Tell a terminal main session about a join (R57 step 5).
- Make the local seat check the only mode (R78).
- Key the harness contract by the name `harness@1`, in the code and in `drafts/harness-contract.md`. Drop the hard-coded provider list (R117 items 2 and 4).
- Move the cx and keep verbs out of reflection into cch. Stop importing `awm.claudedaemon` outside cch (R135 items 4 and 6).
- Make dsh name the calling session (R135 item 3).
- Add a start hook that carries the brief to the harness contract in R135 item 3 (`scopes.md` R143).
- Reword R135 items 2 and 3: the harness names the calling session, and reflection applies trust and mode to it.
- Rewrite the R18 and R51 gap rows in `sessions.md` for cch. Add a gap row for R141.
- Drop the OpenCode line from `AGENTS.md` with the R143 rewrite (R135 item 5).
