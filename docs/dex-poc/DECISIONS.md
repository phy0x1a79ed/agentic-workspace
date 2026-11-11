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

## format-per-locality (2026-11-09)

**Question.** Which encoding does a message use at each locality?

**User's words.** "we should decide on the format per locality now. it may just be 2: struct vs json at the IPC seam" and, on the proposal, "looks good, next?"

**Decided design.** Two formats, split at the process boundary.
- In process: a struct. The envelope (`v`, `id`, `from`, `to`, selector handle, `replyTo`) is always a fixed-layout struct. The args are a typed struct pointer when caller and callee agree on the module's interface hash and layout version, otherwise JSON bytes.
- IPC and network: strict versioned JSON for the envelope and the args. A module's upcasters translate prior versions.
- A layout mismatch falls back to the JSON form, where translation runs. The upcasters touch JSON only. The layout check replaces the separate encoding-negotiation field proposed earlier.
- The struct side is flat, fixed-size fields plus borrowed slices. Richer data rides as JSON bytes inside `args`.

**Why.** Plugin-to-gateway IPC is already JSON frames over HTTP and WebSocket, and each hop costs far more than JSON does. A third, same-layout bytes format would only pay off with shared-memory IPC and would add an architecture, endianness and layout-version matrix to the frozen contract.

**Measured (capella, hot loop, no `dlopen`).** Pointer call through a swappable table 2.2 ns; with memcpy in and out 10.3 ns; strict JSON both ways for a 106-byte request about 400 ns.

**Landed.** Input to `handoff`. Refines `contract-and-builds`.

## calling-mechanism (2026-11-09)

**Question.** How does one call site reach a callee in process, over IPC and across the network, and what carries the legs?

**User's words.** "we need to abstract this so that the caller never knows". Then "compact and build POC, report experiment findings".

**Status.** Working design. The user did not confirm the proposal in words; they set the requirement above and directed the build on the proposal. Revisit if an experiment contradicts it.

**Working design.**
- The caller never knows the placement. `to` is location-free, the router chooses the path, and placement comes only from the registry. A test caller has no placement branch.
- One surface for every placement: `call(to, selector, args, deadline)` returns a future. A local callee gives a future that is ready on first poll with no allocation.
- Across the C ABI: `call(handle, &req, &mut rep)` returns Done or Pending(ticket); `poll(ticket)` collects the reply. A selector resolves to a handle once. No callbacks, because one could outlive a swap.
- A deadline is mandatory on every call.
- One closed error set at every placement: `doesNotUnderstand`, `timeout`, `unavailable`, `moved`. On `moved` nothing has executed, so the router re-resolves and retries once. It surfaces the others.
- The router never retries an effectful call automatically, because the outcome after a timeout is unknown. The envelope `id` is the dedup key. Pure selectors retry freely.
- Carrier in the POC: a Unix socket locally and an ssh channel (stdio, no listening port) to capella, as `awm-edge` does. The existing gateway path is measured as the baseline.

**Landed.** The POC build (`STACK.md`, `poc-build`).

## market-scope (2026-11-11)

**Question.** What kind of job does the first compute market run, and who provides the compute?

**User's words.** "pressing issue is some compute market for awm peers." "can we abstract sockeye and fir, nibi, and trillium as well? slurm's fair use quota's would also be interesting to consider as a system. I think this system should be more standaone than other plugins in that it should be able to be called directly, not just through awm." "aspirationally, Id want compiled nextflow workflows to be executable through this system"

**Decided.**
- A job is general: a command, a requirement predicate, inputs and outputs. A cargo build is the first recipe. (Option B; the user widened it rather than picking A or C.)
- Providers are not only awm peers. The UBC cluster Sockeye and the Alliance clusters Fir, Nibi and Trillium are providers too, behind one abstraction. Slurm's fair-share quotas are an input to allocation.
- The system is standalone: it can be called directly, not only through awm.
- Aspiration: run compiled Nextflow workflows through it.

**Known.** ssh routes to sockeye, fir, nibi and trillium already exist: altair holds the UBC VPN jump, and ssh-arbiter on alcor holds the 2FA leases (journals 06ffb49f, e9f609ba). No awm verb runs a remote command today. The edge times out at 60 s, so a job is submit, then status and result. `dvc run` with its watcher is the closest existing job pattern.

**Landed.** Opens the topics `standalone`, `providers`, `fair-share` and `nextflow` under `compute-market`.

## standalone (2026-11-11)

**Question.** Who calls the market directly, outside awm, and how?

**User's words.** "we could prolly do the same 3 way generative design as awm? which has cli, mcp, and http? the 3 callers you suggest work."

**Decided.**
- Callers now: the shell, awm agents, and Nextflow running as one whole job (the market runs the head process; Nextflow submits its own tasks). Later: a Nextflow executor plugin, and users off the fleet (that needs its own auth layer).
- Surfaces: like awm, each verb is declared once and generated into a CLI, MCP and HTTP. The market daemon owns job state. The CLI is a thin client over the node's Unix socket; `submit` returns an id at once, `wait` and `logs -f` stream events, `fetch` pulls outputs by digest into the shared store, and `run` = submit + wait + fetch with the job's exit code. On a host with no daemon the CLI uses `ssh <node> market serve-stdio`.
- The CLI talks only to its local daemon; the daemon decides placement.

**Open, for `core-boundary`.** awm's generator lives in its gateway crates (`awm-gateway`, `awm-mcp`, `awm-cli`). A standalone market either reuses them (and depends on awm) or the catalog-to-surfaces generator moves into a crate with no awm dependency that both use.

**Landed.** Input to `tool-surface` and `core-boundary`.
