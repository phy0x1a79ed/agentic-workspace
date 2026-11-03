# redux-harness plan

## Context

Tony saw a compaction fail on altair, and the search session's deliverable never arrived because of it. The survey (`compaction-survey.md`, job 8162a704) found the cause: Claude Code's daemon swaps an outdated background worker in place while `/compact` runs, and reflection loses the compaction and the queued follow-up without a trace. After that fix, this lane recuts the harness integration (`install.md` R135): one `reflection` tool that calls the harness plugin of the session, with `cch` for Claude Code and `dsh` for the DeepSeek harness. It also builds `reflection wait` and resumable retire (`sessions.md` R10, R51) and isolated container sessions (`fleet.md` R109).

## What you said

> it looks like compaction fails sometimes. Altair compaction failed for the search session, which is why it was never delivered. We gotta investigate and fix this.

> the cut makes sense to me. Basically, it's just the MCP provides a unified reflection tool that then calls out to either the correct harness to actually do the message injection. This also needs to work for things like social seats. I think maybe a lot of services may end up having to use the harness service at some point.

> no open code

> Local updates only, bring the peers up to date in one sweep when we are done

> just pause if unsure

## Issues

- I1. Reflection deletes the owed-resume record before it types the resume, so a worker that dies after that moment leaves no debt on disk.
- I2. Reflection counts the resume as delivered once the session enqueues it, though the queue lives only in the worker's memory.
- I3. Reflection keys an owed resume by process id, so a session that the daemon respawns under the same session id counts as gone.
- I4. Nothing checks that a `/compact` reached its `compact_boundary`, so a killed compaction looks like a success in every log.
- I5. Every give-up path in reflection writes only to the service log, which nobody reads, so a lost compaction or resume is silent.
- I6. cx and keep both serve the `reflection` tool, and reflection reaches Claude Code directly, so no other harness can plug in.
- I7. Seven services and the gateway import the Claude Code library `awm.claudedaemon` instead of calling a harness plugin.
- I8. Reflection carries an OpenCode lane that awm no longer supports.
- I9. dsh acts only on the harness as a whole, and reflection refuses every dsh session.
- I10. Retire deletes the job, so `resume_other` refuses a retired session, and no `reflection wait` verb exists.
- I11. No session runs in a container, and the gateway finds a caller only by host process ids.

## High-level goals

- G1. A compaction that Claude Code kills is noticed and redone, and the session gets its follow-up.
- G2. When awm cannot recover a lost compaction or follow-up, someone hears about it.
- G3. One reflection tool works for every coding agent harness through that harness's own plugin.
- G4. A session can wait until other sessions finish, and a retired session can come back.
- G5. An untrusted session can run walled off in a container without risk to Tony's login.

## Acceptance criteria

- A test drives an owed resume through a worker swap (new pid, same session id, no `compact_boundary`) and sees reflection re-issue `/compact` and the resume to the new pid.
- A test shows the owed-resume record stays on disk until the transcript shows the `compact_boundary` and the resume taken up, and a service restart in between resumes the watch without typing the resume twice.
- A test shows each give-up path raises an alarm: an ERROR log line, an entry in `reflection pending` with state `lost`, and a journal message to the session's scope.
- `reflection whoami` reports the R9 `session_id`. `start_other` returns the R9 id of the session it started.
- `awm/services/cch/` and `awm/services/dsh/` each carry an `awm-plugin.toml` with a hidden tool (`harness-cc`, `harness-ds`) with `implements = ["8ca45e2d-96cb-497f-8e00-e4cb25ba0fd3@1"]`, the harness contract UUID. reflection's manifest names that contract in `[[needs.contract]]` and reads its providers from `AWM_RESOLVED_TOOLS`.
- reflection imports no Claude Code code. `grep -r "claudedaemon\|oc_" awm/services/reflection/awm` finds nothing.
- No OpenCode code remains under `awm/services/reflection/`.
- journal, reporting, tether, social and transcripts import no `awm.claudedaemon`. They reach sessions through the gateway.
- `reflection wait(until=[ids])` records a wait, and a test shows the waiter resumed once every listed session finishes. A retired job stays resumable by its session id.
- `harness-cc start` with `isolated=true` starts a session in a rootless Docker container whose login file holds only the access token. A test with a fake login checks the file has no refresh token.
- The test suites of reflection, cch, dsh and every service touched pass under `awm/gateway/scripts/run-tests.sh`.

## Tasks

- T1. Write the R9 session id module (G4)
- T2. Keep the owed resume on disk, keyed by session id, until the session takes it up (G1)
- T3. Re-issue a compaction and its resume after a worker swap (G1)
- T4. Raise an alarm on every lost compaction or resume (G2)
- T5. Test and commit the compaction fix (G1, G2)
- T6. Compact
- T7. Write the harness contract, with dsh's side of it (G3)
- T8. Build the cch plugin from cx, keep, claudedaemon and reflection's Claude Code parts (G3)
- T9. Recut reflection to call the harness plugin through the gateway, and drop OpenCode (G3)
- T10. Test and commit the recut (G3)
- T11. Compact
- T12. Move the other services off `awm.claudedaemon` (G3)
- T13. Build `reflection wait` and resumable retire (G4)
- T14. Build dsh's session verbs to the contract (G3)
- T15. Compact
- T16. Build isolated container sessions in cch (G5)
- T17. Draft the doc updates (G1, G2, G3, G4, G5)
- T18. Debrief

## Approach by task

### T1. Write the R9 session id module

Add `awm/services/reflection/awm/reflection/ids.py` with `r9`, `parse_r9` and `normalise`. redux-build/sessions imports it. Platform codes are `cc` for Claude Code and `ds` for dsh.

Gotchas: a dashless UUID that starts with `cc` looks like an R9 id, so `normalise` tests the UUID form first.

### T2. Keep the owed resume on disk until the session takes it up

Change `pending.py` and `inject.py` in reflection. The record key becomes the Claude session id. The pid and its start time become the current address of that session. The record gains a phase: `awaiting_command`, then `resume_sent`. Delivery no longer clears the record. The record clears when the transcript shows the resume taken up and, for `/compact`, a `compact_boundary` after the injection. A replay of a `resume_sent` record watches for that proof and never types the resume again unless T3's swap test says the worker lost it.

Gotchas: the transcript tail resolves its file through the pid, so it must follow the session id when the pid changes. The `.done` stamp and the superseded check are keyed by pid today and move with the key.

### T3. Re-issue a compaction and its resume after a worker swap

In the watcher, a dead pid is no longer the end. Reflection looks up the session id in `~/.claude/sessions/*.json` for a new live pid, for up to two minutes. A new pid with no `compact_boundary` since the injection is a swap that killed the command. Reflection then types the command again into the new worker and waits for it as before. A new pid with the boundary present but the resume not taken up gets the resume again. Re-issue at most twice for one promise.

Gotchas: the swap writes a synthetic "No response requested." with a new CLI version, which proves the swap but must not count as the resume landing. A plain `/cd` or `/clear` changes the transcript file, not the pid, and must not count as a swap.

### T4. Raise an alarm on every lost compaction or resume

Add one `alarm` function in reflection. Every give-up path calls it: delivery rounds spent, re-issues spent, session gone with no successor, the command never confirmed. It logs at ERROR. It appends to a `lost.jsonl` beside the pending records, which `reflection pending` lists with state `lost`. It posts a journal message to the session's scope through the gateway, which copies onto the session's card. When the session is alive, it also types one plain line into it that says what was lost.

Gotchas: the journal post runs from a service with no caller session, so it names the author explicitly. A failed alarm must never raise into the watcher.

### T5. Test and commit the compaction fix

Extend `test_reflection_pending.py` and `test_reflection_watcher.py` with a fake worker swap, a restart mid-watch, and each alarm path. Run the reflection dist. Commit.

Gotchas: the tests fake `/proc` and the session records. They must not touch the live `~/.claude`.

### T6. Compact

Handoff: the plan file, the todo list, and the commits of T1 to T5 on `feat/redux-harness`. Facts a re-read will not recover: the agreed interfaces with sessions (R9 module path, `reflection wait` with required `until`, whoami `task_id` from `awm/services/reporting/awm/reporting/tasks_ids.py`, cd calls `scope claim`), fleet (`harness-cc revive_pinned`, pinned jobs listed as stopped), trust (policy library theirs, ledger and hooks in `reflection/policy.py`), and core (manifest `awm-plugin.toml`, hidden tools `harness-cc` and `harness-ds`, each with `implements` the harness contract `8ca45e2d-96cb-497f-8e00-e4cb25ba0fd3@1`, which Tony chose as option a, install.md R117 item 4).

Agreed with sibling lanes (keep this list current):

- sessions (redux-build/sessions, branch feat/redux-sessions): R9 module at `reflection/ids.py` (mine). `reflection wait` with required `until: list[str]` of R9 ids. whoami reports `session_id` (R9), `claude_session_id` (raw) and `task_id` from `awm.reporting.task_verbs.task_for_session` (lazy import). start_other returns the R9 `session_id`, and passes `task_id` and `role` through to cx start. cd calls `scopes` fn `scope_claim {project, scope, session_id, pid?, job?, queue?, terminal?}` before typing `/cd`, refuses a background move on `ok=false, claimed=false`, carries on with the warning for a terminal session, then `scope_release {project, scope, session_id}` for the old scope. While the verb is absent, cd says `claim: unavailable`. I serve a hidden `reflection` fn `waits {} -> {waits:[{session_id, job, until, since_ms, project, scope}]}`. Release notices reach a waiter through `reflection` fn `message {job, text}`. The retire skill text is theirs, drafted at `docs/redux/drafts/retire/SKILL.md`.
- trust (redux-build/trust, `awm.trustpolicy` at `awm/service_components/trustpolicy`, commit 4c96975c on feat/redux-trust): pure library. `level_for_start`, `may_steer(actor_level, verb, target_level=)` over `STEER_VERBS`, `cross_edge`, `FIRST_LOCAL_MESSAGE_HINT`, `raise_grant`, `grant_key()` reading `<AWM_DIR>/grant-key.pub`. The per-session ledger lives in reflection, behind `reflection/policy.py`. Hooks: cross_edge before any `peer=` start, message or stop; the hint on a session's first awm `message` to a local session; may_steer on steer verbs; a hidden `trust_set` for their grant path. start accepts `trust`, origin `seat:<...>` when the spec name starts with `seat:`.
- fleet (redux-build/fleet): `revive_pinned()` on `harness-cc` (and on `reflection` until the recut) resumes each pinned job whose process is gone, giving it a lineage record when it has none. It returns `{revived, live, failed:[{job, error}]}`. list_other shows a pinned job whose process is gone as `pinned: true, state: "stopped"`. Message fleet when it is committed.
- core (redux-build/core, `plugin-contract.md` v1.2 on feat/redux-core): `awm-plugin.toml` beside `run.sh`. Hidden tools have `tier = "off"`. Tool names use hyphens. Calls go through `POST $AWM_HUB_URL/invoke` with `X-Awm-Plugin`, names resolved from `AWM_RESOLVED_TOOLS`, contract providers under its `contracts` key.

### T7. Write the harness contract, with dsh's side of it

Write `docs/redux/drafts/harness-contract.md`. It names each verb a harness plugin serves (platform, resolve the caller, status, send, interrupt, start, stop, resume, list, set model, effort, name or folder, compact with a done signal, capabilities) and its result. Survey the deepseek-harness fork on altair and capella read only. Write what dsh must serve per session, and what dsh cannot yet do, such as a compaction signal.

Gotchas: dsh sessions live only in the GUI today, so per-session identity may need a token the gateway does not yet carry. Record that as open, not as a design.

### T8. Build the cch plugin

Create `awm/services/cch/` with `awm-plugin.toml`, `run.sh` and a hub adapter that serves the hidden tool `harness-cc`. Move the session roster, the tmux and daemon lanes, the PTY protocol, the daemon unit, the warm pool, the hooks and the settings guard into it from cx, keep, `awm.claudedaemon` and reflection. Keep cx's and keep's verbs reachable under `reflection` during the move, so today's callers do not break. Add `revive_pinned` for the fleet lane.

Gotchas: the gateway's mode gate and caller lookup import `awm.claudedaemon` today. The library stays importable as cch's own code until T12 moves its last outside user.

### T9. Recut reflection to call the harness plugin

reflection keeps identity, trust and mode, the R9 id, the resume ledger and `wait`. It finds a session's platform from its R9 id, or for a caller from the harness plugin that claims the caller pid, and calls that plugin through `POST /invoke`. Remove the OpenCode lane and its tests. Add `reflection/policy.py` as the one seam for the trust lane's library.

Gotchas: the resume watcher of T2 and T3 waits on transcript detail that is Claude Code specific. It moves into cch, and reflection keeps only the ledger and the alarm. Splitting it wrong puts transcript parsing back into reflection.

### T10. Test and commit the recut

Move each test with the code it covers. Add a contract test that runs reflection against a fake harness plugin. Run reflection, cch, dsh and the gateway dists. Commit.

Gotchas: `run-tests.sh` runs only dists listed in its `DISTS` and `ORDER`. Add cch there.

### T11. Compact

Handoff: the contract doc, the cch tree, the recut reflection, and their commits. Record which outside imports of `awm.claudedaemon` remain, from a fresh grep.

### T12. Move the other services off `awm.claudedaemon`

For journal, reporting, tether, social and transcripts, replace each import with a gateway call to `reflection` or `harness-cc`. The gateway's caller lookup asks the harness plugins. Coordinate the gateway change with redux-build/core, which owns the gateway.

Gotchas: journal stamps `meta.session` with the raw UUID. Switch it to the R9 form only with the sessions lane's agreement, because readers match on it.

### T13. Build `reflection wait` and resumable retire

`stop` stops the job and keeps it resumable by its session id. `wait(until=[ids])` stops the caller the way retire does and records the wait in reflection's ledger. A sweep resumes the waiter through its harness plugin once every listed session has finished. `start_other` returns the R9 id.

Gotchas: retire removes jobs today through `claude rm`. Keep the transcript and the job's state when stopping, or the wake has nothing to resume.

### T14. Build dsh's session verbs to the contract

Add `harness-ds` to dsh, serving the contract verbs that the fork supports. Each verb dsh cannot serve answers with a clear `unsupported` error.

Gotchas: altair and capella are read only tonight, so dsh is tested against a fake server only.

### T15. Compact

Handoff: commits of T12 to T14. Note which dsh verbs are live and which answer `unsupported`.

### T16. Build isolated container sessions in cch

`harness-cc start` with `isolated=true` runs Claude Code in a rootless Docker container. It mounts the host's Claude install read only and the scope worktree read and write. It passes a login file with the access token and no refresh token, and a per-session caller token so the gateway can identify a caller that has no host pid. It applies CPU and memory limits and gives no ssh. A host helper rewrites the token copy after each refresh. Its install is a host change, so it lands as a script only.

Gotchas: the login file is a secret. Tests use a fake login and never read the real one. The caller token needs a gateway change, so agree it with redux-build/core.

### T17. Draft the doc updates

Write proposed changes to `install.md`, `sessions.md`, `fleet.md`, `ARCHITECTURE.md` and the retire skill into a "Proposed doc changes" section of this plan. The interview session owns the area docs.

Gotchas: none.

### T18. Debrief

Run the `debrief` skill. Send one line to job 8162a704.

## Still unsure about

- Whether the alarm for a lost compaction should also put the session's card in Blocked. The plan posts a journal message only, which is the reversible option.

## Callouts

- The root cause is inside Claude Code: the daemon should not swap a worker during a local command. awm can only notice and redo it.
- The `feat/fix-reflection-compact` commit `afb46fbb` (the PTY host greeting) is already on `release` and deployed on mizar and altair. It fixes a different failure, the 2026-10-31 greeting timeout, so this branch does not need to merge it. It arrives with the next merge from `release`.
