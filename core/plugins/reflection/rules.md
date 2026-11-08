# Rules for every reflection/dsh port agent

You work in the git worktree `/home/tony/agentic_workspace/projects/awm/port-harness`, branch `feat/port-harness`. Another agent may work in another crate of the same worktree at the same time.

## Sources
- The approved plans: `docs/plugin-redux/ports/reflection.md`, `docs/plugin-redux/ports/dsh.md`, and the plan file `/home/tony/.claude/plans/sparkling-sniffing-sonnet.md` (§ T4 holds the latest reflection blocks, including **Orders**; § T6 holds dsh's **Session events**). Where the plan file and a ports doc differ, the plan file wins.
- The contract: `docs/redux/drafts/harness-contract.md`. The state rules: `docs/plugin-redux/ports/harness-state.md`.
- Finished Rust plugins for style and reuse: `core/plugins/cch/src/` (harness@1 provider, `session_events.rs` is the provider side of the lifecycle events), `core/plugins/dsh/src/`, `core/plugins/routine/src/` (snapshot.rs: the pre-redux snapshot pattern).
- Shared crates, read their public API before writing anything similar: `core/crates/awm-plugin-sdk` (`Service`, `Hub`, `config::{scope_of, node_name, awm_dir, canonical_workspace}`, `privacy`, `procfs`, `ids` for R9 ids, `Service::emitter` + `Context::emit`), `core/crates/awm-harness` (`contract::*`), `core/crates/awm-trust` (`grant::verify_order`, trust policy), `core/crates/awm-statecheck`.
- The describe baselines: `docs/plugin-redux/ports/baseline/`.

## Rules
1. Edit only the files your brief names. Never edit another crate, the SDK, awm-harness, awm-trust or the workspace `core/Cargo.toml`. If you need a change outside your files, finish without it and say so in your report. Your own crate's `Cargo.toml` is yours.
2. Build and test only through docker, from the worktree root: `scripts/rust-build.sh test -p <your package>` and `scripts/rust-build.sh clippy -p <your package> --all-targets`. Never run bare cargo. Clippy must be clean for your files. The `awm-plugin-sdk` `result_large_err` warnings are old: ignore warnings outside your crate.
3. Port behaviour exactly, including timing budgets and refusal texts, except the listed changes in the plan. Port every Python test one to one as a Rust test named after the Python one. Report Python vs Rust test counts.
4. Fixtures: no real ids, names, tokens, prompts or message text. Synthesize every fixture.
5. Async: one tokio runtime. No blocking sleep or blocking IO on an async path. Put each external effect (gateway calls, clock, files tests fake) behind a trait so the Python test fakes port one to one. No timers or polling loops: work is triggered by events (Tony's standing rule). A server-side long-poll that the server wakes on a new item counts as an event.
6. Comments: a `//!` module docstring of one or two sentences, a `///` on each public item saying something the signature does not. Carry over Python comments that state an invariant or a reason. No other comments.
7. Never log a verb's `text` or a message body. Never call a live gateway, never type into a real session, never touch `~/.claude` or `<workspace>/.awm` except to read when your brief says so.
8. Do not commit. Do not use git stash. Leave your files in the worktree.
9. If any command fails with "No space left on device", stop at once, delete nothing, and report it.

## Report
End with: the files you wrote; Python vs Rust test counts; the describe diff against the baseline (only listed changes may differ); anything you could not port or changed on purpose, with the reason; any open question.
