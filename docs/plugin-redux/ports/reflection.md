# Port plan: reflection

## Purpose & Contents

This file holds the approved port plan for reflection, from the harness group's plan (task CWPYAtyb). Tony approved it on 2026-11-06. It is the plan as approved: later changes go in the port commits and the journal. The rules every harness port follows, and the state each one carries over, are in `harness-state.md`. The describe each port is diffed against is in `baseline/`.

## Plan

**Surface.** It keeps all 26 verbs, their params, surfaces and timeouts. Every session verb resolves its target through harness@1 providers. Listed describe additions, from the comms settlement:
- a new `probe {node}` verb, sent to each provider whose capabilities list `probe`, forwarding `X-Awm-As` so cch's peer gate sees the peer;
- `message` answers `code: "gone"` for a missing job, whichever side finds it missing;
- `list_other` rows carry `job`, `session_id`, `parent`, `state`, `live`, `alive`, `kind`, `name` and `transcript`;
- `list_other` gains an `include_stopped` input;
- a failing provider adds an entry to `list_other`'s `errors: [{harness, error}]`, and the verb still answers `ok: true`;
- `start_other` refusals keep their typed `code`;
- `stop` takes an optional `job` that defaults to the caller's own session, like `model`, `effort`, `rename`, `cd`, `interrupt`, `mcp`, `link` and `remote_control` already do. Stopping your own session still requires `confirm: true`. Every self-acting verb resolves the caller through one helper, and a test checks that each one either defaults `job` or takes none.

**State.** `.awm/services/reflection/`, in place. One async mutex guards each file, and none is held across a gateway call. scopes calls back into reflection during a claim, so handlers run concurrently.

**Needs and calls.** It needs only `harness@1`. It calls `scope claims|claim|release`, `operations task_for_session` and `journal brief_read|post|session_parent`, each degrading when it fails. It also calls `retired`, `retired_among` and `list_other` on peers.

**Shared crates.** awm-plugin-sdk, `awm-trust` (gaining the first-message hint and the effort ceiling in the ops land), and awm-harness `contract`.

**Consolidations.**
- One store module.
- `alarm` folds into `ledger`.
- `scope_of` comes from the SDK, a listed change: a `cd` into a worktree subfolder claims the worktree's scope.
- One peers module.
- `session_of` fans out once, in parallel.
- One target-and-steer helper.

**Fixes.** `scope claims` works again, retire notices reach peers, and `.closed` markers get pruned after 7 days. Behaviour change: with D1 fixed, `start_other` refuses a scope another session holds. Seven claims are live now, so routine, social and journal's tagsync can see new refusals.

**Tests.** Port the 78 dispatch tests behind a fake-gateway trait. Add tests for the fixes and the re-entrant claim. Add a gateway e2e test of the caller lookup. For live parity in the sandbox, compare the read-only verbs and `session_of`.

**Landing.** Alone, after the ops `awm-trust` and `awm-statecheck` lands. While reflection is down, every session call waits on it, so keep the rollback ready. Check `whoami`, `list_other` and a `wait` round trip on mizar and capella, then on altair, shogun and lyra when they answer.

Gotchas: read `_caller_session_id` before `_caller_pid`. Never log a verb's `text`.
