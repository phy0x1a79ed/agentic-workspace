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

**Sessions topic** (trilium-design `CONTRACT.md` § port-harness, items 1 and 2).
- reflection declares the emitter `reflection/sessions`. It emits `{event, job, session_id, name, node, project, scope, kind, task_id?, rc_url?, ts}` for `started`, `stopped`, `remote_control` and `renamed`. It emits nothing at boot. A subscriber reads `list_other` once when it connects.
- The providers report these events. `harness_event` gains four lifecycle kinds with the same names, each carrying `{session_id, job, name, cwd, session_kind, task_id?, rc_url?}`. `harness_event`'s own `kind` names the event, so the row's kind travels as the new param `session_kind`. A provider sends lifecycle events only while reflection's describe lists that param. reflection maps `kind` to the topic's `event` and `session_kind` to its `kind`, adds `node`, `ts`, and `project` and `scope` from the SDK's `scope_of` on `cwd`, then emits. A lifecycle kind is not a ticket: reflection keeps no ledger row for it, and a provider does not resend one it failed to deliver. A subscriber that lost reflection reconnects and reads `list_other` again.
- `list_other` rows gain `rc_url` and `task_id`, and every row carries `session_id` (CONTRACT.md item 2, e62fcb43), so a subscriber keys a session it first sees at connect by its id, not its job. A row whose provider cannot name a session id yet, such as a start before its first turn, carries `session_id: null`. Its `started` event follows once the id exists. A test checks that every row, isolated and pool rows included, has the key. `rc_url` is the provider row's `link` while Remote Control is on, and is absent otherwise. `task_id` comes only from the provider row, and stays absent when the row has none. reflection makes no `operations` call for it: `list_other` is called often, and operations already keeps the session-to-task map in `board_changes`.
- Tests: each lifecycle kind gives exactly one emit with the added fields. A failed scope lookup still emits, with `project` and `scope` null. A restart emits nothing.

**Sender names** (Tony's decision, through plugin-redux/interview).
- Every line that reflection hands a provider to type, as opposed to a slash command such as `/compact`, `/cd` or `/rc`, starts with a sender prefix and then the text: `from <session name>@<node> [job <id>]: <text>`. This covers `message`, the `prompt` of `start_other` and `resume_other`, `message_self`, and the follow-ups of `compact` and `message_self`. The caller's own session gets the prefix too, so no agent-typed line reads as Tony's.
- reflection builds the prefix from the caller frame the gateway stamps, never from an argument:
  - a local session: the name and job from `session_of` on `_caller_pid`, or from `_caller_session_id` for an isolated session, plus this node;
  - a session on another node: the door-verified `_caller_session {session_id, node, job, name}`, using the `node` the door verified;
  - a plugin's service: `from <plugin>@<node>: `, with no job, from `_caller_plugin`;
  - a session with no name: its job id is used as the name.
  - a caller with no session and no plugin, such as a shell on the node: `from shell@<node>: `.
  A service relaying a person's words, such as trilium's note button or social's seat line, reads as the service. A service that vouches for a person says so inside its text, as social does with `slack from <sender>`.
  A `text` that already starts with `from ` is typed as given after the real prefix, so a sender cannot forge another prefix.
- `message` takes `to`, which replaces `job` and the `peer` key: `"<session name>@<node>"` or `"<job id>@<node>"`. A bare name or job means this node. The address splits at the last `@`, because names hold `/`. reflection forwards a `to` on another node to that node's reflection, whose door stamps the caller claim. A name that matches two sessions is refused, and the refusal lists the matching jobs. `resume_other` takes `to` the same way.
- Tony's own typed input reaches the session directly and is never prefixed.
- describe: `message` drops `job` and gains a required `to`. `resume_other` takes `to` in place of `job`. The descriptions of `message`, `message_self`, `start_other`, `resume_other` and `compact` state the prefix. These are listed changes in the describe diff.
- Tests: the prefix for each caller kind (local, isolated, peer claim, plugin, no name); an argument cannot set it; a forged leading `from ` is still prefixed; `to` parsing for `a/b@mizar`, a bare name, a bare job and `x@y@z`; an ambiguous name is refused with its jobs; a `to` on another node is forwarded.

**Tests.** Port the 78 dispatch tests behind a fake-gateway trait. Add tests for the fixes and the re-entrant claim. Add a gateway e2e test of the caller lookup. For live parity in the sandbox, compare the read-only verbs and `session_of`.

**Landing.** Alone, after the ops `awm-trust` and `awm-statecheck` lands. While reflection is down, every session call waits on it, so keep the rollback ready. Check `whoami`, `list_other` and a `wait` round trip on mizar and capella, then on altair, shogun and lyra when they answer. On mizar, start a throwaway session, rename it, turn its Remote Control on, then `kill -9` it. Each step shows one `reflection/sessions` event within 5 s.

Gotchas: read `_caller_session_id` before `_caller_pid`. Never log a verb's `text`. The emit never waits on a gateway call. Build each event from the `cwd` that the event carries, not from a new `status` call.
