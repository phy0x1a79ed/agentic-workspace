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
