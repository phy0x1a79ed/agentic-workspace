# Brief R1: port reflection to a Rust core plugin

Read `~/.cache/port-harness-notes/refl-briefs/rules.md` first and follow it.

Port the Python reflection service (`awm/services/reflection/awm/reflection/*.py`, 2.2k lines, tests `awm/services/reflection/tests/test_reflection_dispatch.py`) to a new crate `core/plugins/reflection` (package `awm-plugin-reflection`, binary `reflection`). The workspace glob `core/[cp]*/*` already picks the crate up, so do not edit `core/Cargo.toml`. Path-depend on the shared crates as `core/plugins/cch/Cargo.toml` does.

Your files: everything under `core/plugins/reflection/`. Do not touch `awm/services/reflection/` (the lead deletes it after you finish).

## What to build
1. The plan in `docs/plugin-redux/ports/reflection.md`: **Surface**, **State**, **Needs and calls**, **Shared crates**, **Consolidations** and **Fixes**. Keep all 26 verbs, params, surfaces and timeouts, plus the listed describe changes. The describe must match `docs/plugin-redux/ports/baseline/` (find the reflection snapshot) except those listed changes. Write a describe test as `core/plugins/cch/tests/describe.rs` does.
2. Do NOT build the **Sessions topic**, **Sender names** or **Orders** blocks: brief R2 adds them on top of your crate. Shape your code so they slot in: one caller-resolution helper (the plan's "every self-acting verb resolves the caller through one helper"), one place where the text handed to a provider is built, and the `harness_event` handler as its own function.
3. The manifest `core/plugins/reflection/awm-plugin.toml`: as the Python one, but needs only `[[needs.contract]] harness@1`. Plain calls to operations, scope and journal declare nothing (decision a5f837ac). No `path`/`run` keys: copy cch's form. `run.sh` is one line, `exec "$AWM_DIR/bin/reflection" "$@"`.
4. State, in place, same files, same formats: `<AWM_DIR>/services/reflection/` (`trust.json`, `waits.json`, `retired.json`, `tickets/`, `events.jsonl`, `lost.jsonl`; `pending/` is cch's old promise store, which cch migrated: leave it alone). Write `state.toml` in the shape of `core/plugins/cch/state.toml` (relative paths resolve against AWM_DIR), and take the pre-redux snapshot before the first write as `core/plugins/cch/src/snapshot.rs` does, with a `reflection state <cmd>` subcommand as `core/plugins/cch/src/main.rs` does.
5. One async mutex per file, never held across a gateway call. Handlers run concurrently, because scopes calls back into reflection during a claim (test the re-entrant claim).
6. Callers: the gateway stamps `_caller_pid`, `_caller_session_id` (an isolated session's R9 id; read it before `_caller_pid`), `_caller_session` (a door-verified peer claim `{session_id, node, job?, name?}`), `_caller_plugin` and `_caller_trust` on each call. Find how the SDK exposes them (`awm_plugin_sdk::Call`), and how the Python reads them.
7. Providers: list the harness@1 providers live from the gateway (`GET /hub/contract/harness@1`, see the Python `harness.py`) and call them through the SDK `Hub`. Put every gateway call behind one trait so the Python test fakes port one to one.
8. Write `core/plugins/reflection/INSTALL.md` in the shape of `core/plugins/cch/INSTALL.md`: purpose, state, first start, verify. Load nothing else for it; keep it short.
9. Tests: port the dispatch tests one to one, plus tests for each fix, the re-entrant claim, the `stop` default, and the self-acting-verb helper test the plan names. Run `scripts/rust-build.sh test -p awm-plugin-reflection` and clippy at the end.

## Report
As the rules say, plus: the names of the caller helper, the provider-text builder and the `harness_event` handler, for R2.
