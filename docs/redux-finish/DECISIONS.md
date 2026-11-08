# Redux finish — decisions

One section per closed topic, in the order they close. The open topics are in `STACK.md`.

## inherited (2026-11-07)

**Question.** What does this session start from?

**Tony:** "the original interview had things well scoped. we can copy over their decisions as a starting point. we should then work plugin by plugin. Other related issues can be prioritized on a case by case basis."

**Decided.** The plugin-redux interview's decisions stand as below. Each names its source; read the source when a detail matters. The approved per-plugin plans are `docs/plugin-redux/ports/<plugin>.md` on each worker's branch, and each worker's handoff names what is left.

1. **rust-port** (1c0f4acc). Every core plugin ports to Rust. A typical install needs no Python env. Non-core plugins may be any language and install through the same contract. The port consolidates fragments of one concern into one plugin.
2. **core-set** (1c0f4acc, 2a343d6c). Core: journal, scopes, search, kb, reflection, cch, dsh, operations, dvc, social, transcripts, fileviewer, mic, dev, rlm-browser, browser, routine, channel, dashboard, and the front plugin `auth`. Not core: 2fa, trilium, ssh, vpn, wake, engcyc, rlm-factorio, zotero, claude-science. events folds into routine; channel absorbs relay (relay service only on the public host); httpsfront, auth and tlsfront become one front plugin `auth`; board is not ported.
3. **deps-semantics** (a5f837ac). A `[[needs.*]]` entry is a hard dependency; the loader refuses a plugin without it. Any plugin may call any tool; an absent tool answers `not installed`. A core plugin has no hard dependency on a non-core one. Reserved names: awm, auth, operations, journal.
4. **board-store** (fc14982b). operations owns the queue store on alcor and is its only source of truth. trilium renders it into the vault and turns edits into operations verbs. Cards migrate from the vault at the port.
5. **plan** (5d8bd9db, `docs/plugin-redux/PLAN.md`). Shared library crates live in the core cargo workspace. A plugin never imports another plugin's own crate.
6. **binaries** (c2419412). One static musl binary and one process per plugin. A heavy engine carried by two plugins means a bad cut.
7. **knowledge** (8caec34d, superseding c2419412's guide-into-journal). One Rust plugin `kb` serves journal, search and transcripts. Each source has its own LanceDB under `services/kb/lance/<source>`; guide is a kb source. The Python kb is renamed `papers` and keeps the tool `kb` and the sources kb and library until they migrate on sockeye. Transcripts are full-text only. kb runs on every node as tony; the singleton journal unit and the `awm-journal` user go. scopes lands after kb.
8. **engine** (plugin-redux STACK, 2026-11-07). search is a federator with no content store. Each source owns its engine through awm-index (LanceDB, event-driven maintenance, no timers). Tony: "I really dont want multiple copies", "I really hate timers ... make this event based".
9. **state-safety** (plugin-redux STACK). Tony: "they must not leak pre-redux state". Each plugin's first Rust start snapshots its state to `.pre-rust/` through awm-statecheck. Roll back by reverting code and keeping live state. Restore `.pre-rust/` only on proven damage. Deleting `.pre-rust/` needs Tony's dated OK.
10. **plain-ssh** (plugin-redux STACK). Tony: "base requirement is that there is a plain ssh edge so that `ssh node` works where it is supposed to." The edge rides plain ssh: no ExposeAuthInfo, a service key names itself in its forced command. Never dial a `managed: false` node. Never ssh to fir or sockeye outside the ssh-plugin flow.
11. **reflect-sender** (plugin-redux STACK). reflection prefixes each message with its sender, `from <session>@<node> [job <id>]: <text>`; a destination takes the same form.
12. **orders** (389a0111). Now: operations keeps its own `agent` order loop with `allowed_orders.agent = [alcor]`. End state: reflection pulls its own orders via `order_pull`, with a `pulled_at` lease, then operations drops its loop.
13. **seats** (004f4c13). Adoption keeps an occupied seat's live session and records its Remote Control switch on. The seats stay paused after the land until Tony turns them on.
14. **zotero** (004f4c13). deneb's zotero keeps its timer sync. Tony: "zotero_sync can go".

15. **trilium board** (trilium-design interview, journal a7d480e6; `docs/trilium-design/DECISIONS.md` and `CONTRACT.md` on feat/trilium-design). Tony: "I want ops and the control panel to be pages in trillium, essentially notes". The board, Ops (the swarm status page) and the per-note bot button are vault notes on deneb. alcor is their backend, an API at auth.tony-xy-liu.com, with one sign-in page there (12-hour passkey session, redirect back on success). A new core plugin `telemetry` (Rust) keeps the roster on alcor from events, with no polling. Sections: Needs you, Working, Stale, History; no Proposed, no Swarm triage. One New agent button starts a Remote Control agent with no brief in `awm/lobby`. Approvals and 2fa presses open alcor's passkey modal; everyday actions carry no grant. Seats have a Remote Control switch held by social. The vault writer lands after the Rust operations.

Still open from the original interview (shelved by Tony, "shelf all decisions to your debrief"): R79 vs the vault, per-node-edges, lyra's vpn.toml, the `isolated/` mirror tag, alcor's sites daemon. Each is raised when a plugin's work reaches it. The trilium board adds one: R79 forbids deneb's outbound door calls, which the vault writer needs (trilium recommends alcor pushes through hidden trilium verbs).

## queue-unblock (2026-11-07)

**Question.** The land queue is held by the dead batch cffff968f16f. How do this session's merges reach mizar and alcor?

**Tony:** "ignore the land queue. it is for parallel agentic work and we will do it all this session. so just merge as you normally would."

**Decided.** This session does not use `scripts/land.sh`. The stale lease and the queued tickets 3e3bc12a2040 and 68647284ec63 stay untouched; their branches merge by hand. Per plugin: merge the branch into `feat/redux-finish` (from release), run the tests it touches and `deploy.toml`'s check, rebuild stale pins with `scripts/refresh-pins.sh` and commit them, promote `feat/redux-finish → dev → release`, push `release` to `github` fast-forward, then refresh mizar and alcor with `deploy.toml` § refresh (`refresh-checkout.sh`, then `node-refresh.sh` with `AWM_REFRESH_RESTART`). Other nodes are not refreshed until `later-nodes`.

**Landed.** This file; no code.

## dvc (2026-11-07)

**Question.** What is dvc's scope for mizar and alcor?

**Survey** (`/home/tony/.claude/jobs/b8680c94/tmp/survey/ops.md`). The port is done on feat/port-ops-t7 (34521995); its crates' tests pass merged onto release. Neither node has `dvc.toml`; mizar's two jobs are disabled; alcor has no Globus token and no jobs. No question for Tony.

**Decided.** Merge feat/port-ops-t7 with the known conflict resolutions (deploy.toml pins union plus dvc.pin; run-tests ORDER minus dvc; fleet.md R97 item 1 from t7, item 2 from release). Run the dvc tests and refresh pins. Restart `dvc,routine` on mizar and alcor. Live checks on mizar: `describe`, `status`, `scope_status`, gc dry run, `coverage`, the mirror dry run listing every `.pre-rust/` as excluded; then hooks name the Rust binary, `.awm-py.bak` copies exist, `pgrep globus` is empty. altair's live job and capella's checks wait for `later-nodes`. dvc lands together with operations (one merge of feat/port-ops, which carries t7).

**Landed.** Not yet.

## ops-questions (2026-11-07)

**Question.** Four questions from the ops-group survey: the vault board freeze on deneb (Q1), deneb as an operations caller against R79 (Q2), cutovers whose last checks need Tony at a device (Q3), and claude-science's front behind auth (Q4).

**Tony:** Q1 "this is about deneb. only worry about mizar (and alcor when needed)". Q2 "same". Q3 "we are going to land in plenty of transitions states throughout this work. that is ok. lets just keep our head down and work through it." Q4 "focus".

**Decided.**
1. deneb's board and its R79 question are not this session's work. The deneb caller line in cloud feat/ops-units stays; Tony allowed it on 2026-11-06 ("deneb can join operations callers for now"), and it does nothing until deneb's service key is recorded.
2. Cut over without waiting for Tony. A transition state is acceptable. The passkey and 2fa checks are not gates.
3. claude-science's C2b is out of scope.

**Landed.** Cloud dev 47f8d98: feat/ops-units with the `singleton_rust` map cut to operations only (social and browser join with the comms batch), and Python social's `awm/services/social` folder restored.

## auth (2026-11-08)

**Question.** How does the front plugin `auth` replace auth, httpsfront and tlsfront on mizar and alcor?

**Decided.**
1. One Rust service serves the tools `auth` and `httpsfront` and the browser edge on `AWM_HTTPS_PORT`. The Python auth and httpsfront are deleted.
2. The Python tlsfront library and `awm.config.tokens` stay. claude-science, which is not core, imports them. They go with `python-out`.
3. `[[service.front]]` listeners and `auth front` wait for their first user. No plugin declares a front yet.
4. Tests are curated, not ported: 257 Python tests became 113 Rust tests that run in about 2 s.
5. statecheck's `decls/` drafts are deleted. Its test reads each plugin's real `state.toml`.
6. The root `.gitignore` covers `awm/services/httpsfront/.certs/` and `.sans`. The folder's own `.gitignore` left with the Python, and the deploy's `git clean` would have deleted the leaf on nodes that hold no CA key.

**Parity.** 264 requests against the live Python edge on mizar showed two differences, both fixed: a bare text reply carried a content type, and the sign-in `next=` lost a path holding an encoded `#`. Intended differences: an upstream failure is always 502, the WebSocket bridge splices bytes after the handshake, and the listener offers only HTTP/1.1.

**Landed.** Release 5ebadb0a. On both nodes `awm services` shows auth from `<AWM_DIR>/bin/auth` and no httpsfront. `state check` reports nothing lost, `.pre-rust/` exists, `/ca.crt` matches, an existing-format session opens the portal, a mic WebSocket answers 101, and `auth.db` is vacuumed. Not run: a real passkey sign-in and a phone visit, listed under Waiting on the user.
