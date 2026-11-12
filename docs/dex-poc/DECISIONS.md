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
- Facts are organised as knowledge triples. Each element is a metasmith-style type set from its own namespace: subjects from the data and dependency namespaces, predicates from a relation namespace (`at`, `derived_from`, `reviewed_by`), objects from the place namespace or data again. A place type (e.g. any Slurm scratch with 1 TB free) abstracts location. Revised by `fronting`: a place is a provider, not a node; a place type is a type set over provider properties.
- A cache hit is a fact already true. Moving versus recomputing is decided by bids.
- The unit of work is an offer set: a short chain of steps sharing a place variable L, e.g. move the inputs to L, then run at L. It is biddable when its starting state is true. One provider bids on the whole set and is the binding of L; it may subcontract moves.
- What-planning is done ahead of time by the requester (metasmith's solver, a compiled Nextflow plan). The adapter (e.g. the baitan Nextflow executor) builds offer sets mechanically from a task's inputs and their current places. The market does where-planning. Providers plan preconditions one level deep and price them into the bid.
- No query engine now. A provider checks an offer set with L bound to itself, by type-set subset tests and lookups by who. Facts change only through events; each provider is the authority on facts about itself.

**Deferred (journal 3a6c999a).** A query layer (conjunctive patterns, Datalog as the ceiling) for discovery without a who; per-step combinatorial bids; providers deciding what to compute; actions that cannot be phrased as an end state.

## dependencies (2026-11-12)

**Question.** How are dependencies named and matched, what is the cache key, and does baitan need metasmith's type system?

**User's words.** "lineage is optional..." "cache misses are expensive, but false positive hits are wrong... Can we do caching on exact offers?" "Im also not sure that we need to include lineage constraints?" "need to think through the caching scenarios one at a time to make sure that we can still use dumb caches". "we do need to hash the inputs in some way, I suggested \"block chain\" which refers to hashing outputs by including the hashes of the inputs." "when baitan recieves the compute job, I think the types are large lost and is just a dumb job." "can we set it up as the first layer? exact match hits. later, we can add subsetting as the next layer." "rule 2 makes sense". Tests mock the jobs: "we prolly shouldnt actually run the job, but just mock it instead. this goes for builds as well."

**Terms.** An *instance* is one concrete piece of data or software; its *instance id* is its hash identity. A *type* is a property set. A *fact* is a subject–predicate–object triple; the subject is an instance id. An *offer* asks for a fact to become true; its *offer key* is the hash of its canonical content. ("What" and "who" are retired.)

**Decided** (revised by `cache-identity`, below).
- The baitan core does no planning and runs no metasmith solver. No lineage constraints: what-planning, and metasmith's lineage constraints with it, stay with the requester. Hybrid assembly (MEGAHIT then OPERA-MS) gives no false positive, since Prodigal on each assembly names a different input id.
- Each contract declares which params affect output (rule 2, as metasmith leaves resources out of identity).
- A checker may watch for the plan's own evaluation output (e.g. the plan's CheckM result) and report the review after the pipeline, instead of posting a new job.
- Superseded by `cache-identity`: the exact-match cache keyed on an offer hash, dependencies by exact name and version, and "types as optional facts the core does not interpret". Subset matching is in, not deferred.



## deliverable (2026-11-12)

**Question.** How does an offer identify what it sends back? For a build, is the output "binary is built" or "binary@L"?

**User's words.** "makes sense, next?" (on the proposal below)

**Decided.**
- Outputs are named slots declared by the contract (like metasmith's `AddProduct`). Each slot's instance id is `H(offer key, slot name)`, so a later offer can consume one slot alone.
- A compute step ends at "the output is at L", where L is the provider's own workspace. Settling reports the fact `(slot id, at, place)` and the true size.
- Delivery to a specific place is a separate placement step, added to the offer set only when the requester needs it there (an interactive build on mizar: build at L, then move to mizar). A release rollout is one placement offer per node.
- Keeping compute and delivery apart leaves "built on capella" as a reusable fact and keeps transfer cost visible in bids.

## cache-identity (2026-11-12)

**Question.** How are instances identified and how is a cache hit found, given no content hashing, imports halfway through a pipeline, nondeterministic tools, version bumps, and offers that omit a version?

**User's words.** "scenario 4 points to have a separate thing outside of baitan to find things across nodes, but prolly shouldnt be baitan's responsibility." "there needs to be a way to import things \"half way\" ... reference databases for diamond." "we can use the same index yaml system that metasmith uses ... why 2 modes? ... maybe timestamping isnt important? only what we declare in the type properties set?" "declarations as promises are fine, its how metasmith works." "we dont hash contents, so even if there is variablilty in the output, the promise is the same". "this is the responsiblity of the user not baitan. for checkM, if version is improtant, it goes in the types property set. any version then simply omits it ... I guess we need subsetting after all. can we make this efficient and not a scan?" "Whatver is using baitan can choose to include versioing information like the commit hash, or hash the script themselves and pass it in as a type property". "where do cache hits happen btw? ... prolly only at the provider since we need to knw about partial results." "the hit finds the previous result and the hash is just the identity, not used to determin cache hit, I thikn that holds"

**Decided.**
- Every instance has a property set. A computed result's set is declared by the provider as specifically as it knows: the contract, the input ids, the output-affecting params, and the dependency versions actually used. An import declares its set in a metasmith-style index YAML. Declarations are promises, as in metasmith. Adoption of an existing result is just an import that declares the computed form.
- The instance id is the hash of its property set. It names the instance and lets two providers agree without talking; because input ids are inside the set, ids still chain. The hash is never used to find a cache hit.
- A cache hit is a subset match: the request's property set is a subset of a stored result's expanded set (its own properties plus its input ids, one level deep). A specific stored result answers a looser request (stored version 1.0.2 answers "any version"); never the reverse.
- Cache hits happen at the provider, as bids. Each provider indexes its own results, running jobs and dependencies (an inverted index of key=value to bitmap; the implementation is the builder's call). A hit bids "already done"; partial results and in-flight duplicates bid "done at T". The caller tracks nothing. No global cache.
- Inputs are always exact ids. Relaxing an ancestor ("CheckM on bins from any MEGAHIT version") needs a walk over derived-from links; deferred to the query layer.
- baitan never hashes content. Versions, commit hashes and script hashes are properties the caller chooses to include. Timestamps are bookkeeping in the index, never identity.
- Nondeterministic tools need no handling: the id is the promise, not the bytes.
- Finding results across nodes is a locator service outside baitan.
- Two doors (the same bytes brought in twice) hit only if both imports declare the same set; otherwise it is an accepted miss.

**Open.** Scenario 9: a cached result that fails its review.

## cache-scenarios (2026-11-12)

**Question.** Walk the caching scenarios one at a time to confirm the caches stay dumb. Scenarios 1–3 and 6 were walked under `dependencies`; 4, 5, 7 and 8 under `cache-identity`. Last: what happens when a cached result fails its review?

**User's words.** "reviews cant fail... it only scores the result against the criteria of the bid. a broken promise means the results were never delivered"

**Decided.**
- A review never fails a result. It scores the result against the offer's criteria and feeds the provider's score for that contract. The result stays cached either way.
- A broken promise means the result was never delivered (the offer settled with an error, or never settled). No instance is created, so nothing enters a cache and nothing downstream can depend on it.
- So the cache never needs invalidation and never cascades.

**Landed.** Closes the caching walk. Test cases are in journal 3a6c999a.

## storage (2026-11-12)

**Question.** Where do inputs come from and results go (chinook, altair, provider workspaces), and who pays to move inputs to the provider?

**User's words.** "option A, but requester needs to know where the input is now (not necessarily L)"

**Decided.**
- baitan has no storage concept. A store is a place; putting something there is a placement offer. Revised by `fronting`: places are providers, so chinook and altair's workspace are providers.
- Results reach chinook only through an explicit final placement step that the requester's adapter appends. There is no default sink.
- chinook is a provider that bids only on placement steps. Publishing from altair to chinook is one such step.
- A provider's local workspace (data, dependencies, finished results) is internal to it. Its cache index covers that workspace; baitan never looks inside.
- Input transfer is inside the compute bid (option A): the provider fetches inputs and prices the move into its estimate.
- The requester states where each input is now, as `(input, at, place)` facts in the offer set's starting state. The provider prices the move from that place to its own L. L stays unknown to the requester until the award.

**Landed.** This file.

## locator (2026-11-12)

**Question.** Where does the requester get the current places of its inputs, and does the first version need a locator?

**User's words.** "dont need locator. both expected consumers (agents requesting builds and Metasmith) already know where inputs are and should request to have outputs moved back."

**Decided.**
- No locator. Both expected consumers (agents requesting builds, metasmith) know where their inputs are and state them in the starting state.
- Consumers ask for outputs to be moved back with an explicit placement step.
- A starting state may list several `at` facts for one input; each bidder prices the nearest.

**Landed.** This file. New topic `gc` pushed under `compute-market`.

## gc (2026-11-12)

**Question.** How does a caller get provider-side data deleted once it is done with it?

**User's words.** "we shouldnt make this a part of offers, then we will be deleting intermediates and moving stuff back and forth throughout a pipeline. rather the caller should be able to request disposal of things when job is done. -> this could be formatted as an offer, but prolly can be optimized since only the original providers can fullfil the offer"

**Decided.**
- Retention is not part of an offer. Intermediates stay put through a pipeline.
- When the job (or the whole pipeline) is done, the caller sends a disposal request naming exact instance ids.
- Disposal has the shape of an offer (end state: the instance is no longer at L) but skips the bid round: only the holder can fulfil it, so it goes straight to the provider that settled the instance. The caller knows that provider from the settled events.
- The provider deletes the instance and drops it from its cache index, then settles. An id it does not hold settles as done.
- The caller names only what it wants gone, so reference databases and build tools stay unless named.

**Landed.** This file.

## fronting (2026-11-12)

**Question.** How does a front (altair) bid for backends that cannot run a live service (Slurm clusters, sleeping spot machines), and what does an award trigger?

**User's words.** "slurm commands need to be cached and time blocked (no more than 1 call per min, including sbatch for jobs). Also, it'll be the front's job to manage arrays (scheduling)" / "providers can register at a node and how they fullfill requests are their business. This also means that nodes aren't really improtant here. the locations are really providers." / "for gtdbtk, we'd make the image persist so that the reference always exists. data that is processed doesnt survive though"

**Decided.**
- A provider registers at a node. How it fulfils requests is its own business; baitan sees only bids, awards and settles.
- Nodes drop out of the model. Locations are providers: `(x, at, p)` names a provider, and L binds to the awarded provider.
- A front is just a provider host that registers several providers (one per cluster or spot machine), each with its own bids.
- A Slurm front caches every Slurm command's output and makes at most one call per minute per cluster, `sbatch` included. Bids use the cached queue and fair-share state. The front batches awarded work into job arrays and owns that scheduling.
- The front tracks each backend's holdings from the awards and settles that pass through it.
- Spot machines: the image persists, so reference databases (GTDB-Tk) always exist and bid as present. Processed data does not survive a stop; the front drops those entries when the machine stops, and disposal of them settles at once.
- Award to a sleeping spot provider: the front starts the instance from shogun's image; the instance runs the job and reports through the front.

**Landed.** This file. Absorbs `providers`.

## fair-share (2026-11-12)

**Question.** How do Slurm fair-use quotas enter the market "as a system"?

**User's words.** "offers are scored [on] axes. one of these axes should prolly be time to completion. It may be used nearly always, but im hesitant to force it on everything. A slurm front provider should take into account fair share in the time estimate for the bid. fairshare -> expected queue time + scheduling consessions (if it wants to batch, for example) -> expected total ETA. A provider that needs to batch may delay a bit before bidding to wait for a batch of bids, which are typical for workflows. So offers should be asynchronous, not serial"

**Decided.**
- Time to completion is a common axis, not a required one. An offer names its own axes.
- Fair-share has no axis of its own. A Slurm front turns it into time: fair-share gives expected queue time; add any delay the front accepts to batch; the sum is the bid's ETA.
- A batching provider may hold its bids briefly to collect a batch of offers, as workflows post many at once.
- So callers post offers concurrently, not one after another. The baitan executor must post every ready task at once.
- baitan has no quota or budget system.

**Landed.** This file. The concurrency rule goes into `nextflow`; bid timing goes into `bids`.
