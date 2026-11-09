# DEX POC: Decisions

One section per closed topic, in closing order. Open state is in `STACK.md`.

## freshness (2026-11-09)

**Question.** How does a node learn that what it holds is out of date, given that time-based staleness is not wanted?

**User's words.** "we dont want time based staleness." "transport keepalive is pretty low level and established protocol"

**Decided design.** A name is either subscribed (the node holds what the origin last announced) or disconnected (unknown). There is no `max-age` and no `stale-while-revalidate`. Three events drive updates:

- Publish: the origin announces name, hash and sequence to subscribers, who fetch the bytes by hash and swap. The publish event is the activation unless a node's policy holds it back.
- Subscribe or reconnect: the node sends the hash it holds, the origin answers `304` or the new hash (HTTP revalidation, fired by connecting).
- Use: messages and replies carry the hashes they depend on; an unseen hash tells the receiver to fetch first. This replaces the bounded wait in Q6.

Routing to a singleton needs no freshness check: a "moved" reply or a connection failure triggers re-resolution. Behaviour while disconnected is policy per name: CLAUDE.md keeps its last version, an effectful call fails at the seam. A transport keepalive is the only clock, stays inside the connection layer, and its only output is the disconnected event.

Each mutable name has one origin. Whether that sits within C3 is still to confirm (see `claude-md-sync`).

**Landed.** Input to the `handoff` brief. Revises design section 7 and Q6.

## mvp-scope (2026-11-09)

**Question.** What does the first step contain, and in what order are the test cases worked?

**User's words.** "I'd argue that caches, stale signals (when an update happens), and remote filling are the MVP. we add bidding after." "we should work through each individually as we build up the system though."

**Decided design.** The MVP is the content-addressed cache, the update event (publish and subscribe, per `freshness`) and remote fill. Bidding comes after. Test cases (CLAUDE.md sync, a singleton verb, a build or embedding job) are worked one at a time as the system grows, not bundled into the first step. A hands-on POC is wanted to play with the MVP.

**Landed.** Input to the `handoff` brief. Narrows the `first-step` question: the three workloads are no longer a bundle. Revisits nothing in `freshness`.

## exploration-direction (2026-11-09)

**Question.** How far does the first step go, and what does the test system look like?

**User's words.** "lets do full exploration including prototype POC builds." "lets design a small closed system with multiple dev gateways (set one up on alcor as well?)" "2 plugins, a local and a service?" "test the in process hotswapping of rust binaries, we can then iterate on the core contract that must stay frozen (can we use an existing standard?), caches, evictions, and remote resolution. this also need to distinguish from a remote service vs a remote copy that can be cached"

**Direction (the user's, not yet a design).** Build prototypes, not only docs. The closed system spans mizar and alcor with a dev gateway on each, and two plugins: a local one and a service. It tests in-process hot swap of Rust binaries, then iterates on the frozen contract, caches, eviction and remote resolution. Resolution must tell a remote service (called, never copied) from a remote copy (cacheable).

**Measured.** A dev gateway with empty state takes 8 MB RSS (alcor's production gateway takes 31 MB). mizar and alcor both run glibc 2.39 on x86_64, and the installed `awm` binary is byte-identical in size on both. alcor has no Rust toolchain, and neither has `rustc` on its shell PATH (mizar builds run in a container).

**Landed.** Input to `handoff`. The design proposals that follow from this are on `STACK.md` under "Waiting on the user" until confirmed.

## contract-and-builds (2026-11-09)

**Question.** What encoding does the frozen contract use, and where do builds and the second node live?

**User's words.** "lets use capella for builds, so maybe a second remote awm there instead. capella is much faster at builds." "the frozon contract are for dex calls that could cross binary, process, or network boundaries. Lets just use json, honestly. and fail on any unknown keys. -> would also need versioning so that prior calls still get translated and handled during transition periods."

**Decided.**
- The contract is JSON. A message with an unknown key is rejected. Every message carries a version, and a module translates prior-version calls so they still work through a transition period.
- Builds run on capella. The second node is capella (dev gateway there), paired with mizar. alcor can join later as a third node that never compiles.
- The contract covers dex calls across a binary, process or network boundary. Calls inside a module are ordinary code and stay outside it.

**Measured on capella.** 16 cores, 67 GB free, glibc 2.35, native `cargo` and Docker present, production gateway at 10 MB on port 7819. mizar and alcor run glibc 2.39, so an artifact built on capella loads on both; the reverse would fail. capella's `awm` binary differs from mizar's.

**Open, on `STACK.md`.** Whether a reply is downcast to the caller's version, and the in-process encoding.

**Landed.** Input to `handoff`.
