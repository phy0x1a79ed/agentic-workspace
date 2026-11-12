# AWM + DEX: Open Questions

Third of three documents. The first recorded objectives, constraints and
terminology; the second stated the mechanism the discussion settled. This one
records what it did not settle, in the order the answers block work, with the
options and the consequence of each. Nothing here is a decision. Where a
recommendation appears it is mine, marked as such, and Tony may overrule any of
it.

Labels: **Open** (nobody decided), **Blocking** (later work cannot start until
it is answered), **Deferred** (Tony set it aside), **Needs Tony** (the sources
disagree or the call is his), **Inferred** (not stated in the sources;
reconstructed to fill a gap, and Tony's to confirm or change).

Provenance and known limits are as stated in the design document, section 15,
and are not repeated here.

## How to read this

| Group | Questions | What the group blocks |
|---|---|---|
| A. The seam | Q1–Q5 | Whether the in-process design exists at all |
| B. The swap | Q6–Q8 | Whether a swap is safe to ship |
| C. The market | Q9–Q13 | Whether allocation works across heterogeneous nodes |
| D. Porting AWM | Q14–Q18 | The order and shape of the port |
| E. DEX | Q19–Q22 | Where DEX's own work goes |
| F. Brief-traced costs | Q23–Q26 | Appended after E for reference stability; logically (Inferred) Q23 ∈ A, Q24 ∈ B, Q25–Q26 ∈ D |
| G. Brief-traced findings | Q27–Q30 | Appended for the same reason; logically (Inferred) Q27 ∈ A, Q28–Q29 ∈ E, Q30 ∈ C |

Questions are ordered by what they block, not by importance. A question in group A
can invalidate parts of the design document if answered against its recommendation,
so group A comes first even where its answer is cheap. Every question states what
it blocks, so Tony can triage by cost. Groups F and G are kept separate so that
every cross-reference elsewhere in the set stays valid; the "logically" column says
where each question would sit if the document were renumbered.

---

## A. The seam

### Q1. Which loader — dynamic musl, user-space ELF, WASM, or subprocess? **Blocking, Needs Tony**

The design recommends a dynamically-linked musl host. The brief records why the
question exists: the prototype's *static* musl host refused with "Dynamic loading
not supported", while a static host has no dynamic linker image to load against.
Dynamically-linked musl `dlopen`s, and musl ships a real dynamic loader, so the
glibc concern in C1 is not the obstacle it might appear to be.

| Option | Keeps static host | In-process | Consequence |
|---|---|---|---|
| Dynamic musl | No | Yes | Loses the single-static-binary install property. C1 is about portability, not size, so this may be acceptable — but it is a product decision, not a technical one |
| User-space ELF loader in Rust | Yes | Yes | Real work: TLS (`__tls_get_addr`), relocations, static initializers, thread-local destructors, libc interposition. Keeps both properties. Unverified which crates in this space are mature enough |
| WASM modules | Yes | Yes, sandboxed | Per-call overhead and a typed boundary; a second substrate alongside Rust modules. Gains a sandbox that the design otherwise does not have |
| Subprocess seam | Yes | No | The IPC overhead C5 exists to avoid, and the design's 76 ns claim becomes a microseconds claim |

**What it blocks:** sections 4, 5 and 7 of the design doc describe an in-process
seam. If the answer is subprocess, the resolution hierarchy still holds but L0
becomes IPC and the call-cost argument weakens. If the answer is WASM, the purity
split gains a substrate dimension the design does not currently have.

**Recommendation:** dynamic musl, with the user-space loader as a fallback if the
install property turns out to be load-bearing. I cannot verify from here whether
a static host is a real requirement or a by-product of the current build.

### Q2. Is "develop the system in the system" a property of the combined core, or only of DEX's layer? **Blocking, Open**

The design doc assumes one message model over Rust handlers is sufficient, and
leaves a live handler substrate as an option rather than a requirement. That
assumption is only correct if live definition is not needed at the AWM core.

- **If DEX-only:** the combined core needs no eval, no embedded scripting, and no
  live substrate. One substrate — Rust modules over a C-ABI or WASM boundary —
  and the design is as written.
- **If combined-core:** the core needs a live substrate. The options are the
  `dlopen` path (already prototyped, needs dynamic linking), WASM modules
  (sandboxed, slower), or an embedded scripting language (Lua, Rhai, QuickJS —
  small, and the handler is untyped, which inherits E3).
- **If neither:** the feature is dropped, and DEX loses its founding principle.

**What it blocks:** the number of substrates the core must carry. Two substrates
is a real complexity cost that the design doc currently mentions but does not
price.

**Recommendation:** DEX-only. DEX already has TypeScript, which is a dynamic
language that runs the system. Duplicating that in AWM's core buys the principle
twice at twice the cost, and the compile-cost driver does not need it.

### Q3. How does a selector declare purity, and is the declaration checked? **Blocking, Open**

The design makes purity the hinge of caching: pure selectors get the resolution
hierarchy and result caching, effectful ones get routing only. Nothing yet says
how a selector states which it is, or how a false declaration is caught.

- **Declaration as part of the contract.** Purity is a field in the interface
  hash, so a module cannot change it without becoming a new module. Cheap, and
  consistent with P2. Consequence: a lie is an ABI-level claim and is trusted
  the way any hash is trusted.
- **Declaration plus test.** A registered pure selector is exercised against a
  second call with identical arguments and checked for an identical answer. Weak
  — it catches nondeterminism, not side effects.
- **Declaration plus audit.** A cooperative peer may re-execute and compare, as
  with builds. Catches more, costs a run.
- **No declaration; cache nothing by default.** Safest, and loses most of the
  hierarchy's value.

**What it blocks:** whether the result cache is safe to enable at all. Code
resolution is safe regardless; result caching is where the correctness hazards
live.

**Recommendation:** declaration in the contract, with a peer audit available and
not mandatory. The cooperative assumption (C2) makes the audit the same kind of
check as build verification.

### Q4. Where does the TS–Rust seam sit, and what does it cost? **Open**

The design states that some dex calls are IPC and that the TS kernel does not
know whether a selector is answered by JS, local Rust or a peer. It does not say
where the boundary is crossed or what that costs per call.

The POC figure of 2 to 5.6 µs per envelope is a JavaScript artifact, not a
property of the envelope. A call from TS into Rust will pay whatever the chosen
bridge costs — a `napi` or `wasm-bindgen` boundary if embedded, a WebSocket or
Unix socket frame if a peer. These differ by orders of magnitude and the choice
determines whether the TS kernel can be the display path or only a client of it.

**What it blocks:** the navigator's architecture, and whether TS participates in
the hierarchy at L0 or only at L2/L3.

**Recommendation:** measure before designing. Three candidates — embedded in the
same process, same host over a Unix socket, remote over the network — and the
choice follows the measurement. Do not inherit the POC's microseconds as if they
were the floor.

### Q5. Which binaries share a process? **Open**

The design leaves AWM's process boundaries as they are and recommends each plugin
stay its own process, loading its own core modules, with plugins reaching each
other through the router. That is not decided.

- **One process per plugin** (current, recommended): keeps the crash domain
  separate, keeps restart-on-byte-change cheap, and pays IPC between plugins.
- **All plugins in one process:** minimal call cost, shared crash domain, and a
  segfault in any handler takes the host down.
- **Grouped by trust or by hot-path:** a hybrid; more complex, and the grouping
  rule has to be stable under change.

**What it blocks:** the value of Q1. In-process loading only buys nanoseconds if
the caller and callee share a process. If plugins stay separate processes, the
Rust module seam is inside one plugin and the cross-plugin seam remains IPC
whatever the loader is.

**Recommendation:** keep one process per plugin for the first increment. Revisit
when there is a measurement showing cross-plugin IPC is the bottleneck, which the
brief does not currently show.

---

## B. The swap

### Q6. Is the version-skew window acceptable, and what enforces a minimum version? **Blocking, Open**

Between publication and activation, calls get the old answer. The design's
mitigation is that the answer carries the version hash that produced it. What is
not settled is whether a caller may require a minimum, and who refuses.

- **No requirement.** Skew is accepted; the system is eventually consistent.
  Simple, and unsafe for a caller that needs a new behaviour.
- **Caller requires.** A send carries a minimum version; a node that cannot meet
  it waits, and the call blocks. Correct, and reintroduces the blocking the design
  set out to avoid (P6).
- **Caller requires, with a bounded wait and a `doesNotUnderstand`.** Blocks for a
  bounded time, then fails visibly. Keeps the failure at the seam rather than
  hanging.
- **Node policy only.** The node decides; the caller cannot require. This is what
  the deferred score question would settle, so it is coupled to Q9.

**What it blocks:** whether the stale-while-revalidate model is safe for callers
that need new behaviour, and whether the design needs a blocking path at all.

**Recommendation:** bounded wait with `doesNotUnderstand`, and only where the
caller asks. It keeps the default non-blocking and makes the exception explicit.

### Q7. Which restart rule is correct? **Needs Tony**

The sources disagree, and this is live in the built system, not only in design.

- The end-state record and the name-table section of the plugin contract say the
  landing agent names what restarts, even if that disagrees with the file changes.
- The contract's runtime section and the 11-08 build findings say a plugin
  restarts only when its bytes changed.

The design doc proposes the second: content change causes activation, and a
landing agent may name an activation but cannot force one for unchanged bytes.
That contradicts an existing record, so it needs a decision rather than a
recommendation.

**What it blocks:** the activation path in section 7, and whether a landing agent
can be a source of restarts at all.

### Q8. How does state cross a swap? **Blocking, Open**

Neither system has an image, and this is the piece the design names as unsolved.
The prototype showed the problem directly: a module's state belongs to its
version, so it must be host-held or handed over as bytes during a swap. Globals
and thread-locals are not rebound by the loading mechanisms the prior art uses.

Options, none cheap:

- **Handover protocol.** The old module exports its state; the new module imports
  it, with a version tag so the import is checked. This is `BEAM`'s
  `code_change` callback — the only widely deployed solution to this problem, and
  the reference to read before designing anything.
- **Host-held state.** State lives outside the module entirely; modules are pure
  over it. Clean, and pushes a large refactor onto every plugin.
- **Replay.** The state is reconstructed from the message log, as DEX already
  does for persistent state. Works for persistent state, not for transient state
  the log does not carry — which is the POC's E3 problem in another form.
- **Accept loss.** Transient state is lost on swap; only disk state survives, as
  today. Cheapest, and it is what the run-time process swap already does.

**What it blocks:** whether in-process swapping is safe for any plugin that holds
in-memory state, which is most of them.

**Recommendation:** read `BEAM`'s `code_change` first, then choose between
host-held state and a handover protocol. Accepting loss is viable as the first
increment because it matches what the built system already does.

---

## C. The market

### Q9. Who sets the score, and what wins when caller and node disagree? **Deferred**

Tony deferred this on 2026-11-08; it returns when the bidding discussion opens.
The design records the conflict: node-side policy says "test node, take new source
early"; caller-side says "this send needs at least version X". When they conflict,
something must give.

Coupled to Q6, because a caller requirement is a score from the caller's side.

### Q10. What is the capability predicate's format? **Blocking, Open**

The design requires a requirement over advertised capabilities — CPU, memory, GPU,
ABI baseline — rather than over task type. Nothing says what the vocabulary is or
who extends it.

- **Fixed vocabulary in the contract.** Small, stable, and every new capability is
  a contract change — the expensive kind.
- **Open vocabulary, matched by string.** Extensible, and a typo silently narrows
  the bidder set.
- **Extensible with a versioned schema.** A capability declaration names its
  schema version; matching requires compatible schemas. This is the same move as
  the envelope version and the interface hash, applied to capability.

**What it blocks:** heterogeneous allocation (C6) and the GPU example. Without a
shared vocabulary, a GPU node cannot advertise itself in a way a submitter can
match.

**Recommendation:** extensible with a versioned schema, for consistency with the
rest of the design.

### Q11. How is numerical work verified? **Open**

A deterministic build verifies by output hash. A GPU numerical job may not be
bit-identical across devices. "Deviations are bugs" (C2) is clean for builds and
needs a definition here.

- **Tolerance.** The bid declares a tolerance; the verifier checks within it.
  Requires the submitter to state one, and a loose tolerance is a silent quality
  loss.
- **Redundant execution.** Two nodes run it; agreement within tolerance is
  acceptance. Doubles the cost, and it is the same shape as rebuild-and-compare.
- **Trust the first result.** Fits C2, and abandons verification for that class.

**What it blocks:** whether the market is general or builds-only.

**Recommendation:** redundant execution within a declared tolerance. It is the
same mechanism as build verification, so it does not add a concept.

### Q12. Is accepting one bid per item by the posting session too centralised? **Open**

The design records that a session cutting its own graph and accepting one bid per
item is a buyer accepting an offer for its own build, not an authority over the
network. That framing was mine, and it is not settled.

- **Session accepts.** Simple, and it is the only party holding the graph.
- **Bids form a consensus without the poster.** Truly decentralised within a build,
  and it needs a protocol the design does not have.
- **First valid bid wins.** No acceptance step; the poster is not in the loop. Cheap,
  and it removes the poster's control over assignment quality.

**What it blocks:** C3's scope. If C3 means "no central scheduler" only among
sessions, the design is compliant. If it means "no chooser anywhere", it is not.

### Q13. What is the cache retention policy? **Open**

Per-worktree target directories reached 549 GB in the brief. A content-addressed
cache grows monotonically until something evicts, and eviction on a small station
is a real constraint.

- **LRU by size.** Simple; evicts a large artifact that is used rarely.
- **Pin by need.** Keep what a module's declared needs require; evict the rest.
  Matches C4's "a declared need is hard".
- **Never evict on stations; evict on builders.** Stations hold small artifacts;
  builders hold the churn. Follows from the placement decision.

**What it blocks:** whether stations can hold L1 at all. A 2/8 station with a
small disk cannot be an L1 cache under LRU if the artifact set is large.

---

## D. Porting AWM

### Q14. What leaves the SDK, and what happens when a core service is down? **Blocking, Open**

The design doc's candidate split — connect/register/call/reply/emit stays at the
floor; privacy, telemetry, usage, holders, push and sessions become core services
— is a proposal by an agent in the 11-08 session, not a decision. And it carries
a consequence that session named: core services become critical at run time, so if
privacy is down, gated verbs must refuse.

The refusal rule is the harder half. A handler that depends on a core service
being up needs a defined failure mode, and the design does not have one beyond
`doesNotUnderstand`.

**What it blocks:** the SDK split, which is the concrete form of O2.

### Q15. Is compilation triggered at registration or on first call? **Open**

The design recommends registration, so the call path is uniformly fast and a
module is either fully loaded or absent. Lazy only wins if most registered modules
are never called, which for plugins is not the case. Tony has not stated a
preference.

**What it blocks:** nothing structural; it is a policy the mechanism must support
either way, and it can change without redesign.

### Q16. How are unique names enforced? **Blocking, Open**

The design requires enforced uniqueness for location-free addressing, because the
POC found silent reply loss to be its most expensive bug class and it followed
from addresses that could collide. "Enforced" is not designed.

- **Namespace per node, assigned at registration.** Collisions impossible within a
  node; cross-node addressing carries the node.
- **Content-derived names.** A selector's name includes its module hash, so a
  collision is a hash collision. Very unlikely; names become long.
- **Central registry.** Rejected by C3.

### Q17. Are `host/*` capabilities enforced or conventional? **Open**

DEX's E3: a live-defined handler has full access to a node's internals, with no
interface separating them. If a live substrate is added (Q2), or if in-process
modules share a process (Q5), capabilities must be enforced rather than
conventional or the isolation the plugin boundaries provide today is gone.

- **Enforced by substrate.** WASM or a user-space loader gives this for free; the
  `dlopen` path does not.
- **Enforced by convention.** Matches today's separate-process model, and it is
  the thing that breaks when processes merge.

### Q18. Do the four DEX protocol gaps close before more views? **Open**

Input routing with pass, capture and focus; per-invocation persistence; node
lifecycle; the invisible painter cache. The design notes these are properties of
the contract, not of TypeScript, so they reappear in Rust if unclosed. The POC
recommended closing them before more views; that is advice, not a decision.

---

## E. DEX

### Q19. How does the envelope version stay live (E1)? **Open**

The design makes `v` a routing dimension so two envelope versions can coexist.
That is a direction, not a design. It needs a rule for when a sender may use a new
version, and what a receiver that does not know it answers — presumably
`doesNotUnderstand`, which is the same answer as an unknown selector.

### Q20. Where does the navigator live, and what is the cyanoverse data shape? **Open**

The 11-06 interview on this was opened and not settled. The recommendation there
was a new scope `dex/navigator` on altair, branched from `feat/edge`. A survey
found no document defining the "final folder format" of cyanoverse; AB48 is a
per-level tree and the 2,844-sample cohort is laid out by type and mostly
offloaded.

Carries C8: `feat/edge` is local only and must never be pushed.

### Q21. Is the display relay a separate host? **Open, constraint stated**

C9 says the POC server is not deployable as a relay — it runs any Python a client
sends. A public display host must be display-only. What that host runs is not
designed.

### Q22. Distributed compute plugin — what is it? **Deferred**

Tony: "a manual script for now and a distributed compute plugin later." Later is
not defined, and the market design may make this plugin the market's own
executor rather than a separate thing.

---

## F. Additional questions traced to the brief

These belong in groups A, B and D above; they are appended rather than
renumbered so that references elsewhere stay valid.

### Q23. What is the exact ABI surface, including async completion? **Blocking, belongs in A**

The design doc states the floor as `(interface hash, data-layout version)` but
never states the exported functions that carry a call. The prototype's surface
was `awm_send(selector, bytes, out buffer)` and `awm_free` — synchronous and
blocking — and the brief already records why that cannot be final: "Async sends
need a completion callback, because a send doing I/O cannot block a plugin's
`tokio` thread. Each module has its own runtime."

Options:

- **Sync floor only.** The floor is blocking; async is built above it as a core
  service. Small floor, and the caller pays a thread per in-flight call.
- **A completion-callback variant.** `awm_send_async(selector, bytes, cb, ctx)`.
  One more function at the floor, and callback lifetime across a module swap is
  a new hazard.
- **A handle-and-poll variant.** The send returns a handle; the host polls.
  Fits the hierarchy (fill is async, invoke is sync) and adds state the host must
  track.

This is the one item the brief calls out as not cheaply changeable later: "The
exact floor: the C-ABI functions and message semantics. This is the part that
cannot cheaply change later." It should be answered before the prototype is
promoted, not after.

**Recommendation:** sync floor plus handle-and-poll, because it keeps the floor
at two or three functions and matches the design's async-acquire / sync-invoke
split. Reject the completion callback: a callback that outlives a swap points
into an unloaded module.

### Q24. How is a bad activation recovered from? **Blocking, belongs in B**

The brief's tension 8: "A system that rewrites itself can break itself. The DEX
discussion proposed a boot image, a safe mode and rollback through history. AWM
has rollback by pin and a crash breaker." The design doc's swap section covers
how a good module is activated and says nothing about a bad one.

- **Pin rollback.** Activation names a hash; a bad one is reverted by naming the
  previous hash. Cheap, reuses the pin, and needs a human or a health check to
  notice the badness.
- **Health check before commit.** A new module must answer a probe before it
  becomes the resolution target. Catches crash-on-load and a subset of wrong
  answers; costs a warm-up call.
- **Safe mode.** The router loads a known-good set and refuses activations until
  cleared. This is the gateway's existing crash breaker extended to activation,
  and it is the only option that survives a module that crashes the host on load
  — at which point the process that would revert is already dead.

**What it blocks:** whether activation can be automatic at all. Without an
answer, `activate` is a command that must be supervised by a human, which is
defensible but should be stated.

**Recommendation:** health check plus safe mode, because in-process loading
shares a crash domain and the crash breaker already exists in the built system.

### Q25. How is the seam tested when the compiler no longer checks across it? **Open, belongs in D**

The brief: "The compiler stops checking across the boundary. Each service needs a
test at its seam." The design moves checks from compile time to seam tests and
never says what a seam test is.

- **Contract tests generated from the interface hash.** The hash covers the
  signature and layout; a generated round-trip test catches mismatch at load.
  Cheap and mechanical.
- **A conformance suite per module.** Broader, and it is authored rather than
  generated, so it drifts.
- **Peer audit at run time.** A cooperative node re-executes and compares, as
  with builds. Catches what tests miss, and costs a run.

**Recommendation:** generated contract tests as the gate at load, with peer audit
available. This is the same division as Q3: mechanical check at the seam,
cooperative audit behind it.

### Q26. Does this work follow the redux landing, interleave, or run in parallel? **Open, belongs in D**

The brief, section 4: "Whether the work follows the current redux landing. The
agent recommended it does." And separately: "the live `redux` session received
advice on smaller decoupling steps. Its uptake is unknown."

- **After the landing.** Cleanest; the design doc's first increment measures a
  gateway that is not changing underneath it.
- **Interleaved.** The SDK split (Q14) is redux work and belongs to the landing;
  the envelope re-expression could follow.
- **Parallel.** Fastest, and it risks measuring a moving target.

**Recommendation:** interleaved. The SDK split is the concrete form of O2 and is
worth doing inside the landing whether or not the rest proceeds; the increment
that tests latency and non-recompilation waits until the landing settles.

---

## G. Final questions traced to the brief

### Q27. What is the unit of messaging — view, datum, or call? **Open**

The brief's tension 6: "At 76 ns, the AWM prototype and the DEX envelope at
microseconds are different mechanisms, so the numbers do not compare directly.
DEX's lesson is that the unit of messaging must be chosen by cost." And the POC's
own assessment: pure message passing holds "but its unit is a view, not a datum",
each envelope costing about 15 to 20 times a plain recursive build, so a tip, gene
or atom is data inside its view rather than a node.

The combined design inherits the choice without stating it. If the system messages
per datum, it inherits the POC's 15–20× tax. If per view or per call, it does not.
Nothing in the design doc says which, and the answer differs by substrate: a Rust
call at 76 ns can afford per-datum messaging where a TS envelope cannot.

**What it blocks:** the cost model for the combined system, and whether DEX's
microsecond envelope is a constraint on the whole design or only on the JS path.

**Recommendation:** state it per layer. Per-call on the Rust seam; per-view at
the DEX language boundary. Do not choose one unit for both.

### Q28. What is the public interface for editing a dispatch table? **Open**

The DEX POC's K6: "Editing a dispatch table has no public interface." The design
generalises the dispatch table to the cache directory and has `define` edit it,
but K6 says there is no designed way to do this — it is the gap the POC found and
did not close.

- **A `define` message, as the POC used.** Uniform with the message model; leaves
  `define` itself unvalidated.
- **A typed registration call.** Checked at the seam (Q25); less uniform.
- **No public interface; only module activation.** The dispatch table changes only
  when a module is activated. Smallest surface, and it removes live handler
  definition from the design entirely — which couples to Q2.

**What it blocks:** Q2's answer, because "develop the system in the system" needs
a way to write to the dispatch table, and this is that way.

### Q29. How do data nodes migrate between kernels? **Open**

The POC recommended "decide how data nodes migrate" and recorded that "migration
conflicts with large transient data". This is distinct from Q8: Q8 is state across
a version swap inside one host, migration is data across hosts. DEX's `emigrate`
was partly working; the design doc does not carry it forward at all.

The tension is that content addressing makes artifacts cheap to move and large
transient state expensive to move, so a migrating node either carries its data and
pays, or leaves it and breaks its own references.

**What it blocks:** whether DEX's "identical cores on every host" includes moving
a live node's data, or only loading the same code everywhere.

### Q30. How does a prototype feature run in a few select sessions? **Open, Decided mechanism undesigned**

The brief, section 3 dev: "Tony wants an opt-in mechanism so a prototype feature
can run in 'a few select sessions rather than fleet wide'. The feature is Decided
and the mechanism is undesigned."

This is likely the same machinery as Q9's node-side score — a test node taking new
source early is a fleet-scale version of a select session opting in. But it may
not be: a feature flag per session is finer-grained than a module version per
node, and the design has no concept of either.

**What it blocks:** the first increment's ability to be trialled narrowly, which
is how Tony said he wants to proceed — "a large change that I'd want to test and
develop incrementally."

**Recommendation:** resolve with Q9 if possible. If they are one mechanism, the
score policy doubles as the opt-in; if not, the design needs a second one, and
that is worth knowing before the increment.

---

## The three that gate the most

If only three are answered now:

1. **Q1, the loader.** It can invalidate the in-process seam the design rests on.
2. **Q8, state across a swap.** It decides whether in-process swapping is safe for
   any plugin with in-memory state.
3. **Q14, the SDK split.** It is the concrete form of the driver — compile cost —
   and it is the thing that can be done first, cheaply, and is worth doing
   whether or not the rest of the design proceeds.

My recommendation on order: Q14 first, because it pays for itself immediately and
the first increment needs it either way. Then Q1, then Q8.

---

## Revised gating list

The earlier "three that gate the most" omitted the floor itself. Corrected:

1. **Q23, the exact ABI surface.** The brief names it as the part that cannot
   cheaply change later, and the prototype's synchronous surface is known to be
   insufficient.
2. **Q1, the loader.** It can invalidate the in-process seam the design rests on.
3. **Q8, state across a swap.** It decides whether in-process swapping is safe for
   any plugin with in-memory state.

To start work rather than unblock it: **Q14 (the SDK split)**, which pays for
itself immediately and is needed by the first increment either way.

---

## Completeness criterion

This document is complete against its sources when every item in design §13, every
item the brief marks Open, Untested or CAUTION, and every tension in the brief's
section 7 is either answered by the design doc or appears here as a question.

That trace is now done. The mapping:

- Design §13's eleven items — Q1–Q22, with Q23 correcting §13's omission of the
  ABI surface.
- Brief §4 "Untested costs" — glibc/musl (Q1), async completion (Q23), module
  state per version (Q8), only panics contained (Q5, Q24), no compiler across the
  seam (Q25), core services critical (Q14). "Old modules stay mapped" is a known
  cost with no decision to make; it is recorded in design §7 and not reopened.
- Brief §4 "Open" — process sharing (Q5), the exact floor (Q23), distribution
  (Q22), order against the redux landing (Q26).
- Brief §5 DEX findings — E1 (Q19), E3 (Q17), K6 (Q28), the four protocol gaps
  (Q18), unique names (Q16), migration (Q29).
- Brief §7 tensions — 1 (Q6), 2 (Q2, Q19), 3 (Q5), 4 (Q1), 5 (Q8), 6 (Q27),
  7 (Q2, design §12), 8 (Q24).
- Brief §3 dev — the opt-in mechanism (Q30).

Two items are recorded as known costs rather than questions, because there is no
decision to make: old modules stay mapped, and a static host cannot load. The
second is a constraint that drives Q1; the first is a limitation with no remedy.

What this criterion cannot cover: if the sources miss something, this document
misses it too. Where it is silent on a point, it is because the brief and the
design conversation are, not because the point was judged unimportant.