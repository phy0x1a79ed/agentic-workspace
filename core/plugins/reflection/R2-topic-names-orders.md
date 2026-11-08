# Brief R2: sessions topic, sender names and orders on the Rust reflection

Read `~/.cache/port-harness-notes/refl-briefs/rules.md` first and follow it.

Brief R1 ported reflection to `core/plugins/reflection` (package `awm-plugin-reflection`). Add three blocks of plan § T4 (`/home/tony/.claude/plans/sparkling-sniffing-sonnet.md`, also `docs/plugin-redux/ports/reflection.md`): **Sessions topic**, **Sender names** and **Orders**. Your files: everything under `core/plugins/reflection/`. Read the crate first. R1's report names the hooks it left for you:
- the caller helper: CALLER_HELPER
- the provider-text builder: TEXT_BUILDER
- the `harness_event` handler: EVENT_HANDLER

## 1. Sessions topic
Build the block as the plan states it. Points to keep:
- Declare the emitter `reflection/sessions` in `awm-plugin.toml`, in the form other plugins use (grep `core/plugins/*/awm-plugin.toml` and the SDK for `emit`).
- `harness_event` gains the param `session_kind` and the four lifecycle kinds. The describe change is listed. cch and dsh open their gate when this param is present: read `core/plugins/cch/src/session_events.rs` and `core/plugins/dsh/src/lifecycle.rs` for the exact body they send.
- A lifecycle kind writes no ledger row and is never a ticket. The emit never waits on a gateway call. Use the `cwd` the event carries for `scope_of`.
- `list_other` rows: `rc_url` (the provider's `link` while Remote Control is on), `task_id` from the provider row only, and `session_id` on every row (null when unknown).

## 2. Sender names
Build the block as the plan states it, through the caller helper and the provider-text builder, so there is exactly one place that prefixes. `message` takes `to` and drops `job` and `peer`. `resume_other` takes `to` in place of `job`. Update the descriptions and the describe test's listed changes.

## 3. Orders
The shape is the current one on `feat/port-ops` (commit dfb00c33). Read these with `git show dfb00c33:<path>`:
- `core/plugins/operations/src/approver.rs`: `order_pull` and `order_done`. `order_pull {node, kinds, wait_s}` long-polls up to 55 s and returns an array of signed envelopes. `order_done {order_id, ok, note}` answers `{order_id, recorded}`.
- `core/plugins/operations/src/service.rs`: `order_loop` and `agent_start`. This is today's puller, in operations. Port its behaviour into reflection: same `start_other` args as `agent_start` builds (`task_id`, `role: main`, `remote_control: true`, `suggest: true`, `model`, `effort`, `started_by`, the `note_id` prompt), plus `name` from the order per the plan.
- `core/crates/awm-trust/src/grant.rs`: `verify_order(envelope, public_key, node, now)`. Your branch has `awm-trust`; check its `verify_order` exists here and has this signature. If it does not, stop and report.

Rules:
- Call operations through the gateway as `operations order_pull` with `kinds: ["agent"]` and `wait_s: 55`. A held long-poll is the push channel: no timer between pulls. After a failed pull, retry after a short capped backoff (as `order_loop` does). Start the loop in `on_start`.
- The grant key comes from the env `AWM_APPROVER_GRANT_PUBKEY` (find how operations reads it, the encoding included). With no key, run no puller and log one INFO line.
- Record each `order_id` in `orders.json` in the state dir before calling `start_other`. A recorded id runs nothing. Prune entries older than the order TTL. Add `orders.json` to `state.toml`.
- A bad envelope: one WARN line, no `order_done`.
- The sender prefix for an order's prompt is `from operations@alcor: `, set through the provider-text builder, not by hand.
- Each order ends in exactly one `order_done`. A failed `order_done` goes to a spool file and is resent at the next successful pull.
- Never log an order's prompt text.

## Tests
The plan lists the tests for each block. Write all of them, behind the fake-gateway trait. Run `scripts/rust-build.sh test -p awm-plugin-reflection` and clippy at the end.

## Report
As the rules say, plus each describe change you made, and anything in the plan that you could not build as written.
