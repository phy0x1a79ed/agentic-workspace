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

## braavos-offer (2026-11-11)

**Question.** What does a work offer carry, how is work identified, and how are winners picked and judged?

**User's words.** "lets call this plugin braavos". "we prolly want 2 parts: a what and who. what is \"metasmith compiled nextflow job\" while who is the actual job hash." "we could do lineage and type? like a block chain? Im trying to avoid content hashes like blake3 ... too slow". "we should design the system that would allow this, but prolly not consider loops and conditionals on the first pass". "the offer should choose actually, and if we make bids just return teh vector, we can hot swap the algorithm." "another provider with better performance should win the bid if they are sufficiently better, even if not cached." "the check(er) is also part of the job offer ... then sends back result to the provider as a \"review\"". "we can have the requester estimate size and the provider send back a \"true\" value to improve the requester estiamtes over time"

**Decided.**
- The work itself is open-ended. An offer names a *what* (a type, e.g. a metasmith-compiled Nextflow job, an assembly) and a *who* (the instance id).
- Identity is lineage and type, a Merkle chain: a derived who is a hash over the transform, its parameters and the whos of its inputs. Bytes are never hashed. Only leaves need a stamp, minted once at their origin and carried, never re-derived on the receiver. Metasmith moved leaf identity off blake3-over-bytes onto abspath+mtime for the same reason (altair journal f38459a9); its stat ids are host-specific, so a braavos leaf stamp must name its origin.
- Function and format are separate axes. A requirement names a function; providers satisfy it in different formats (a container on one node, a flat install on another).
- A job may fracture into child jobs; whos keep the execution graph a DAG. Loops and conditionals are allowed by the design but not built in the first pass.
- Moving data is a job like any other.
- Bids return a vector of predicted values on the offer's axes. The offer carries its own appraisal axes and its own winner-picking algorithm, which can be swapped without touching providers. A cached result does not win automatically: a sufficiently better provider beats it.
- The offer carries its checker. The requester runs it on the result (possibly as more jobs, e.g. CheckM2 or seqkit) and sends a review to the provider. Reviews accumulate into a provider's score per what.
- The requester estimates input size; the provider reports the true size back, so the requester's estimates improve. Bids scale with size.

**Leaning, revisit at `bids`.** Score each axis as a robust z (median and MAD over the bids and history), clamp it so no single axis (such as a zero-cost cache hit) dominates, and take a weighted sum.

**Open.** Whether braavos forks metasmith, lifts its code, or takes its ideas (topic `metasmith-reuse`).

## metasmith-reuse (2026-11-11)

**Question.** Does baitan fork metasmith, lift its code, or take its ideas?

**User's words.** "take metasmith ideas then, can be new tool." "braavos is slightly overused, what do you think about baitan (摆摊)?"

**Decided.**
- Take metasmith's ideas, not its code: types as property sets with subset subtyping, lineage signatures, and transform contracts (requirements, products, resources). They become a new tool, extended with a format axis and per-what appraisal axes. Metasmith stays as it is and can join later as a provider.
- The plugin is renamed baitan (摆摊, a street stall). Sections above that say braavos mean baitan.

**Also settled in this exchange.**
- Checkers need flexibility. Design against the case where the checker's CheckM run and the Nextflow plan's own CheckM step must share one cache entry. Several reviews per job and trust are deferred.
- Deferred items and test cases are journalled in 3a6c999a (tag `deferred`), to be edited as they change.

**Landed.** New tool; no metasmith fork.

## promise (2026-11-11)

**Question.** What is the back-and-forth between a requester and providers, in what order, and where does state live?

**User's words.** "we would need a new baitan executor, which is why I pushed for the plugin to be more stand alone". "the key offer, bid, award, started, settled is good. events being broadcasts back also makes sense, but may also need remote state that is pollable for catchup. review after 2nd checker transaction is also right." "we can take some liberties during implementation like we may not need to explicitly push events back to CLI."

**Decided.**
- The key messages are offer, bid, award, started and settled. The requester's daemon holds the promise; the provider holds the job.
- Events are broadcast back to the requester. The provider also keeps remote state that the requester can poll to catch up after a disconnect.
- The promise settles on the result. The review follows after the checker's own transaction (which may be more offers, e.g. CheckM).
- Implementation may take liberties, e.g. the CLI need not receive pushed events.
- A baitan Nextflow executor is in scope (it routes each Nextflow task through baitan), and is why baitan is standalone. This amends `standalone`, which had it as later.

Sequence drawn in the session: run → offer to all → bids → picker → award/decline → provider pulls inputs → started/progress → settled → requester fetches outputs → checker → review.

**Open, moved to new topics.** Where inputs come from and where outputs go (`storage`); whether data transfer, deployment and dependency acquisition are offers (`mutation`, `dependencies`); fronting sleeping providers (`fronting`).

## mutation (2026-11-12)

**Question.** Are data transfer, deployment and publishing offers, and how does that square with pure transforms?

**User's words.** "mutations are also offers, this is great. I think what is missing is a subject". "location needs to be abstracted, then we get predicate logic." "we'd need basically the format of beliefs as knowlege triples in knowledge bases... can each element of the triple be represented by metamisth type sets? but from different namespaces I think." "basically an offer can be bidded on if the starting state is true ... need chain offers here, or offer sets? \"move all of these things locally and execute the actual job\"" "we can prolly avoid planning for the most part, or atleast having to do it ourselves." "it looks like we dont need querries at this stage, but the kb triples structure is still important to organize around."

**Decided.**
- Every piece of work is an offer: compute, data transfer, deployment, publishing. There is one kind of offer.
- The subject is a location-free who; its lineage identity stays pure. A fact is a triple, e.g. `(subject, at, place)`. An offer asks for facts to become true; offers are not pure, whos are.
- Facts are organised as knowledge triples. Each element is a metasmith-style type set from its own namespace: subjects from the data and dependency namespaces, predicates from a relation namespace (`at`, `derived_from`, `reviewed_by`), objects from the place namespace or data again. A place type (e.g. any Slurm scratch with 1 TB free) abstracts location.
- A cache hit is a fact already true. Moving versus recomputing is decided by bids.
- The unit of work is an offer set: a short chain of steps sharing a place variable L, e.g. move the inputs to L, then run at L. It is biddable when its starting state is true. One provider bids on the whole set and is the binding of L; it may subcontract moves.
- What-planning is done ahead of time by the requester (metasmith's solver, a compiled Nextflow plan). The adapter (e.g. the baitan Nextflow executor) builds offer sets mechanically from a task's inputs and their current places. The market does where-planning. Providers plan preconditions one level deep and price them into the bid.
- No query engine now. A provider checks an offer set with L bound to itself, by type-set subset tests and lookups by who. Facts change only through events; each provider is the authority on facts about itself.

**Deferred (journal 3a6c999a).** A query layer (conjunctive patterns, Datalog as the ceiling) for discovery without a who; per-step combinatorial bids; providers deciding what to compute; actions that cannot be phrased as an end state.

## dependencies (2026-11-12)

**Question.** How are dependencies named and matched, what is the cache key, and does baitan need metasmith's type system?

**User's words.** "lineage is optional..." "cache misses are expensive, but false positive hits are wrong... Can we do caching on exact offers?" "Im also not sure that we need to include lineage constraints?" "need to think through the caching scenarios one at a time to make sure that we can still use dumb caches". "we do need to hash the inputs in some way, I suggested \"block chain\" which refers to hashing outputs by including the hashes of the inputs." "when baitan recieves the compute job, I think the types are large lost and is just a dumb job." "can we set it up as the first layer? exact match hits. later, we can add subsetting as the next layer." "rule 2 makes sense". Tests mock the jobs: "we prolly shouldnt actually run the job, but just mock it instead. this goes for builds as well."

**Terms.** An *instance* is one concrete piece of data or software; its *instance id* is its hash identity. A *type* is a property set. A *fact* is a subject–predicate–object triple; the subject is an instance id. An *offer* asks for a fact to become true; its *offer key* is the hash of its canonical content. ("What" and "who" are retired.)

**Decided: layer 1, exact match.**
- The offer key hashes the contract and its script version, the input instance ids, the output-affecting params, and the dependencies by name and version. Resources, provider, place, dependency format, axes, picker, checker and size estimate stay out. Each contract declares which params affect output (rule 2, as metasmith leaves resources out of identity).
- A result's instance id is the key of the offer that made it, so ids chain through their inputs to origin-stamped leaves. A result records its parents' ids one level up; full provenance goes in the results package or a log.
- The cache is one table from offer key to instance. A hit is hash equality: no false positives, only misses when the same work is phrased differently.
- The baitan core is type-agnostic. Dependencies are matched by exact name and version, through a matcher that can be swapped. Types are optional facts the core stores but does not interpret. No lineage constraints: what-planning, and metasmith's lineage constraints with it, stay with the requester. Hybrid assembly (MEGAHIT then OPERA-MS) gives no false positive, since Prodigal on each assembly has a different key.
- A checker may watch for the plan's own evaluation output (e.g. the plan's CheckM result) and report the review after the pipeline, instead of posting a new job.

**Deferred: layer 2.** Subset matching of dependencies and places by metasmith-style type sets.
