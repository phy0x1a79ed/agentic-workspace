# AWM + DEX: Design

Second of three documents. The first recorded objectives, constraints and
terminology; this one states the mechanism the discussion settled, and marks
plainly what it did not. The third records the open questions with their options.
Date, version and author are Tony's to set.

Labels: **Built** (runs today), **Decided** (Tony settled it; not built),
**Direction** (Tony stated the intent; detail open), **Open** (nobody decided),
**Inferred** (not stated in the sources; reconstructed to fill a gap, and Tony's
to confirm or change). **Pending** marks a decision waiting on Tony.
Where this document is silent, it is because the sources are, not because the
point is unimportant.

## 1. The design in one paragraph

One message model serves AWM, DEX and the compute market. A message carries an
identity, a destination and an opaque body; the router reads only the
destination. Code is organized into **modules**, which are content-hashed
compilation and loading units, and **selectors**, which are the individual
functions inside them and which declare themselves pure or effectful. Every send
resolves through a cost-ordered hierarchy — loaded module, local artifact cache,
peer cache, remote fill — and the caller never names the level. The market is not
a level of that hierarchy; it is the **fill path**: the expensive mechanism that
populates the cheap ones, and the only place work is allocated across the
network. Compilation is a class of work in that market, and compilation happens
away from the node that will run the result. A node loads modules in-process, so
the steady-state call is a function call. Publication of a new module and its
activation are separate operations, which is what lets a running system change
without a restart and without blocking a call.

## 2. Principles

These are the discipline the design follows. They are the common thread between
the three lines.

**P1. The frozen boundary is small.** The smaller the contract, the cheaper it is
to move and the less it constrains what sits above it. Tony, 2026-11-08: "The
only coupling is a contract. Moving the contract is expensive, so we declare a
low level one."

**P2. Identity is a hash of content.** A module, an artifact and a work item are
named by what they are, not by where they came from or when they were made. This
is what makes caching correct without invalidation and allocation possible
without a central view.

**P3. Nothing above the boundary is scheduled globally.** Nodes advertise what
they hold and what they can do; work becomes available when its inputs exist;
a node bids on what it can start. There is no fleet-wide scheduler.

**P4. Cheap first, expensive last.** Resolution asks the cheapest place before
the next, and the expensive path exists to make itself unnecessary next time.

**P5. Purity is declared, not inferred.** A selector states whether it is pure or
effectful. Caching, and therefore the hierarchy, applies only where that
declaration permits it.

**P6. Change does not block a call.** A call is served by the loaded module. New
code is acquired and activated on the system's time, not on the caller's.

## 3. The message

**Envelope (Direction).** The DEX proof of concept's shape is adopted as the
reference:

- `v` — envelope version, a first-class routing dimension rather than a constant
  compiled into anything. The POC's failure (E1) was that the version was baked
  into the bundle, so a new envelope contract could not go live. Here the router
  dispatches on `v`, so two envelope versions can coexist.
- `id` — message identity, for correlation.
- `from` — set by the sender's host, never by the sender. In AWM terms this is
  where the caller stamp lives: node, lane, session, trust, scope.
- `to` — the only field the router reads for delivery. Local and remote take the
  same path.
- `selector` — the function name within the destination module.
- `args` — opaque body. Typed only by the contract of the destination.
- `replyTo` — where the answer goes. Absent for fire-and-forget.

**Addressing (Direction).** `to` is a location-free address. That requires
enforced unique names: the POC found silent reply loss to be its most expensive
bug class, and it followed from addresses that could collide. Uniqueness is a
requirement of this design, not a convention.

**Failure semantics (Direction).** An unknown selector, an absent module, or a
missing capability answers `doesNotUnderstand`. This replaces AWM's `not
installed` at call time and is the same construct at every level.

**Sync and async by layer (Direction).** Acquisition of a module may be
asynchronous. Invocation once the module is loaded is synchronous where the
substrate permits it — a function call, in Tony's words "like basically a
function call". Tension 1 in the brief, function call against asynchrony, is
therefore resolved by layer rather than by choosing one.

## 4. Units: module and selector

The design separates the loading unit from the purity unit. This separation is
the answer to the hybrid question Tony posed: "code within a module is code, but
'dex code' is really a collection of pure functions with dynamic fill options."

| Unit | Granularity | Identity | What it governs |
|---|---|---|---|
| Module | Coarse, a compilation and loading unit | Hash of its source and its build inputs, per target | Loading, swapping, caching of artifacts |
| Selector | Fine, one function | Name within its module | Purity, caching of results, capability |

**Purity contract (Direction).** Each selector declares pure or effectful.

- **Pure** — deterministic in its arguments, no observable side effects. Eligible
  for result caching and therefore for the resolution hierarchy. The archetypal
  dex call.
- **Effectful** — routed but never cached. The hierarchy still supplies the
  module; it does not supply the answer.

A module exports a mix. "Dex code" is not a module type; it is the set of pure
selectors, wherever they live — a Rust module, a TypeScript kernel, or a peer.
This is the seam Tony asked to pin down, and this is the answer the design
offers: **the seam is not between two kinds of module, it is between two kinds of
selector, and purity is declared per selector.**

**Why this matters for O2 and O3.** A module's pin hashes its own inputs and not
its dependents' build state. A module that reaches into a shared crate is the one
that pays the rebuild, every time. Isolated builds therefore become the cheap
path rather than a rule to be enforced. A dependency such as LanceDB becomes its
own module behind its own contract because that is what the economics reward.

## 5. Resolution

**The hierarchy (Direction).** Every send is resolved against these, in cost
order, filtered by capability:

| Level | What | Cost | Notes |
|---|---|---|---|
| L0 | Loaded module, in-process | Nanoseconds | The steady state. Prototype: 76 ns including copy in, allocation, copy out, free |
| L1 | Local artifact cache on disk | Local I/O | Content-addressed. The brief's per-worktree targets reached 549 GB, so retention is a real concern |
| L2 | Peer artifact cache | Network | Fetch by hash. Requires nodes to advertise what they hold |
| L3 | Remote fill | Network plus compute | The market. Slowest, and the only level that allocates work |

The caller names what it wants. The system finds the cheapest place it exists.

**Cache-first (Direction).** Resolution checks for an artifact whose hash matches
this source and this target before any bid is opened. Without this, every call
would pay a network round trip for code that has not changed. Most sends should
never reach a compiler.

**The market is not a level (Direction).** You cannot hit on compute. L3 is the
**fill path** for the levels above: it runs when L0, L1 and L2 all miss, and its
output is written back so the second identical request hits. That is what makes
this a hierarchy rather than a slogan: the expensive path is defined by being the
path that populates the cheap ones, and locality is what keeps it from firing.

**Two caches, kept apart (Direction).**

- **Code resolution** — which module answers this selector. Clean, because
  modules load once and stay, and unchanged code always hits.
- **Result caching** — what a pure selector returns. Safe only where purity is
  declared. This is where correctness hazards live, which is why P5 exists.

**The dispatch table is the cache directory (Direction).** DEX maps selector to
handler. Generalised, it maps selector to resolution path, and `define` edits it.
This is why the AWM loader and the DEX dispatch table are one structure rather
than two that happen to resemble each other.

**Coherence (Direction).** Content addressing removes the invalidation problem: a
changed module is a new key, not an invalidation of an old one. Old entries age
out. This is why the pins are the right foundation, and why the unsolved piece is
state across a swap, not cache coherence.

## 6. The compile path

**Shape (Direction; field set Inferred).** Every work item has the same form,
whatever the work is. The field set below is **Inferred** from section 8 and the
compile flow; Tony did not state it, and it may change before it is adopted:

```
WorkItem {
  id           // hash over the fields below, so identity is content (P2)
  inputs       // the content hashes that must be available before this can start
  payload      // opaque; the kind of work and its arguments
  requirement  // capability predicate (Q10)
  verify       // how the output is accepted (Q11)
  output       // expected output identity, where the work is deterministic
}
```

Compilation is one instance of it. Tony: "a message send first resolves to a
compile this for that station's target. A 3rd compile node wins the bid, receives
the source, runs the build, and sends the binary to the original target and
continues from there."

```
compile:
  inputs:      source hash, toolchain hash, target triple and ABI baseline
  payload:     the source and the target
  requirement: target triple, ABI baseline, compiler capability
  verify:      output hash — a deterministic build (Q11)
  output:      artifact hash
```

A GPU numerical job is the same form with a different `payload` and a tolerance in
`verify` (Q11). A container job is the same form with an image reference as its
`payload`. Because the form is shared, the market does not need to know what kind
of work it is allocating.

This is the archetypal dex call: pure, expensive, content-addressed. The market
is the fill path for exactly this kind of call, and it does not need to know that
the call is a compilation. Compilation, GPU inference and a heavy parse have the
same shape and differ only in cost and in requirement.

**Placement (Decided by constraint C4).** Compilation runs out-of-process, on
capable builder nodes. Loading runs in-process, on the target. A 2/8 station does
not run a compiler. This split is also why the compiler's memory spikes and any
build-script execution are outside the runtime's crash domain.

**Artifact contract (Direction).** A module exposes a contract before it can be
called: an **interface hash** and a **data-layout version**. A change to either
is a new module, not a reload. This follows the `fidius` pattern — a trait
becomes a stable, versioned, hash-checked entry point with a typed proxy on the
host side — and it is forced by what the existing implementations document:

- a reloadable function cannot change signature, and generic functions cannot be
  reloaded at all;
- a changed struct layout is an ABI break even when the signature is identical,
  because size and alignment move.

The contract is therefore `(interface hash, data-layout version)`, and that pair
is the frozen floor for loading. It is small enough to freeze.

**Trigger (Direction, recommended; Open).** I recommend compilation at module
registration rather than lazily on first call, so the call path is uniformly fast
and a module is either fully loaded or absent. Tony has not stated a preference.
The cold cost is paid once, where it is visible.

**Verification (Direction).** The target checks the artifact hash and the
interface hash: this is the right output for this source and this target. It
cannot check by hash alone that the compiler did the right thing, which is where
C2 matters — a second node can rebuild and compare, and a deviation is a bug. The
compiler is therefore itself a work unit with a verifiable output, and a bad
compiler is detected rather than trusted.

**Requirement is the target's ABI baseline, not only its CPU (Direction).** The
brief records the trap: the build image has glibc 2.36 while capella and lyra
have 2.35. A binary is valid for a triple *and* an ABI baseline, so that pair is
what a node advertises and what a work requirement matches on. This is why
`heterogeneous-work-units` and `source-to-module` are one design.

**Prior art (reference, not dependency).** Nix remote builders, `sccache` and
`distcc` all do "send inputs, a remote node builds, fetch the artifact by hash".
Replacing a static builder list with a bid is the delta. `Subsecond` thin-links
only the modified functions into a patch library and applies it to a running
process; `hot-lib-reloader` reloads a library into a running program. The design
sends source where Subsecond sends compiled patches, because a heterogeneous
fleet cannot assume the sender and receiver share an architecture or a toolchain.

## 7. The swap

**Publication and activation are separate (Direction).**

- **`put(hash, artifact)`** — a content-addressed write. Idempotent: the same
  bytes twice leave the same world. Effectively pure, and therefore a legitimate
  dex call.
- **`activate(hash, policy)`** — a command. Genuinely impure, and it should be
  visible as exactly one such operation rather than smeared across the protocol.

A swap is therefore not a call into the module. It is a change in what resolution
returns. Publish, decide by policy, activate, and subsequent sends resolve to the
new module. This reframing is what makes the workflow coherent: the impure part
shrinks to one named operation.

**Serving during a swap (Direction).** The loaded module answers; the new
artifact is acquired in the background; activation happens at the next resolution
boundary. This is stale-while-revalidate, a proven pattern, and it has a known
consequence.

**Version skew (Open).** Between publication and activation, calls get the old
answer. The mitigation is that the pin does double duty: the answer carries the
version hash that produced it, so a caller that needs a minimum version can see
it is getting an older one and refuse and wait. Whether that window is acceptable
in general is Open, and is the first question for the third document.

**Restart rule (needs Tony).** The sources disagree. The end-state record and the
name-table section of the plugin contract say the landing agent names what
restarts, even if that disagrees with the file changes. The contract's runtime
section and the build findings say a plugin restarts only when its bytes changed.
The design proposed here is the second: **content change is what causes a
restart or an activation; a landing agent may name an activation but cannot force
one for unchanged bytes.** Stated as a proposal because it contradicts one
existing record.

**What a swap costs (known, from the prototype and the prior art).**

- Old modules stay mapped until the process restarts, because unloading Rust is
  unreliable. Reload is fine; reclaiming memory is not.
- Panics are contained by `catch_unwind`. A segfault, abort, out-of-memory kill
  or stack overflow takes the process down. In-process loading means the crash
  domain is shared; this is the price of C5 and should be a stated decision.
- Reloading from the same file path silently keeps the old code, so each version
  needs its own filename. The design's content-addressed naming satisfies this
  automatically.
- Globals and thread-locals are not rebound. State across a swap is Open; the
  `BEAM` `code_change` callback, which transforms process state between two
  versions of a module, is the reference to read before designing it.

## 8. The market

**The work item (Direction).** Identity, requirement, payload hash, verification
rule. One schema serves container jobs, external programs and compilations.
**Pending:** now that section 6 states the schema, this sentence may become a
cross-reference to it. Tony to decide.

**The requirement is a capability predicate (Direction).** Tony's example: a node
building a semantic-search vector index is more than 10 times slower than an idle
node with a GPU, a completely different task with the same allocation problem.
The predicate must therefore be over advertised capabilities — CPU, memory, GPU,
ABI baseline — not over a task type. Two regimes follow:

- **Capability-exclusive** — only some nodes can bid at all, because the others
  literally cannot execute the work.
- **Capability-comparable** — many can bid, and price or speed decides.

One mechanism handles both if a bid is a function of capability match, price and
expected duration. This is the `JASDA` pattern: subjobs submit scored variants,
the scheduler clears locally, and the mechanism holds without global system
knowledge or lookahead. The GPU example is stronger evidence for this than the
build DAG, because it forces the requirement to be a predicate rather than a
dependency edge.

**Bidding without a central scheduler (Direction).** Tony: "the scheduler can not
be a centralized entity." The mechanism:

1. A session cuts its own dependency graph into work items. The session holds the
   graph; nobody else does. Decentralization is among sessions, not within one
   build.
2. Each item names its inputs by hash. Availability is the gate.
3. Nodes advertise the output hashes they hold. They bid only on items whose
   inputs are available to them.
4. The graph orders itself: an item cannot start until its inputs exist.

The best bid is often "I already hold this output", which costs near zero, and
most posted items should clear that way. The auction matters for the cold
minority. This is what makes a 2/8 station viable: it never has to win a cold
build, only cache hits and small units.

**What remains from the earlier framing, and what was corrected.** An earlier
draft had the session assign partitions after bidding, and had releases gated by
trust tiers. Both were corrected: assignment is not the session's job (C3), and
cooperation makes reproducibility a verification property rather than a
permission (C2). What survives is that the posting session accepts one bid per
item — that is a buyer accepting an offer for its own build, not an authority
over the network. Whether even this is too centralised is Open.

**Verification by work class (Open).** A deterministic build verifies by output
hash. A GPU numerical job may not be bit-identical across devices, so it needs a
tolerance or redundant execution. "Deviations are bugs" is clean for builds and
needs a definition for numerical work before the market is general.

**Score (deferred).** Which source a node chooses to fill from — "this is a test
node, take new source early" versus "this is a release, take it now" — and
whether a caller can require a minimum version. Tony deferred this on 2026-11-08.

## 9. The loader

Static musl cannot `dlopen`; the prototype's host refused with "Dynamic loading
not supported". Dynamically-linked musl can, and musl ships a real dynamic
loader, so C1 — portability without glibc version mismatches, including on a
Raspberry Pi — is satisfiable without glibc at all.

| Option | Keeps static host | In-process call | Cost |
|---|---|---|---|
| Dynamic musl host | No | Yes | Loses the single-static-binary install property |
| User-space ELF loader in Rust | Yes | Yes | Real work: TLS, relocations, static initializers, libc interposition |
| WASM modules | Yes | Yes, sandboxed | Per-call overhead, typed boundary, a second substrate |
| Subprocess seam | Yes | No | The IPC overhead C5 exists to avoid |

**Recommendation: dynamic musl.** The install property is a convenience; the
in-process seam is the point. **Open** — Tony has not chosen, and this gates
whether a module can be loaded in-process on a station at all.

One caveat on the numbers: the prototype measured 76 ns for a send that included
copy in, allocation, copy out and free, and its swap test ran 5.8 million calls
from another thread. The DEX envelope's 2 to 5.6 µs is a JavaScript figure, not a
property of the envelope. A Rust router over a struct payload should land near the
prototype; the comparison should be measured, not assumed.

## 10. How AWM maps onto this

The gateway is already a router: it routes calls, supervises plugins and owns no
tables. The mapping is close to a renaming, which is why this is a generalisation
rather than a port.

| AWM today (Built) | This design (Direction) |
|---|---|
| Gateway | Router |
| Plugin | Node |
| Tool with a verb | Selector |
| `{verb, args, peer}` MCP fold | A routing rule over the same selectors |
| Caller stamp: node, lane, session, trust, scope | `from` metadata, stamped by the host |
| `not installed` at call time | `doesNotUnderstand` |
| 19 pinned binaries | Core router plus handler modules, each its own compilation unit |
| Plugin restarts on byte change | Activation of a new content-hashed module |

**What this delivers against the objectives.** The core — router and envelope —
is frozen; each handler compiles separately. A one-line SDK change no longer
rebuilds nearly everything (O2). A dependency behind its own contract stops being
rebuilt by its dependents (O3). The run-time hot swap already built becomes one
case of a general mechanism rather than a special one (O4).

**What it does not deliver.** AWM's current separations are process boundaries.
Moving them in-process trades isolation for call cost. Which binaries share a
process is **Open**, and the earlier recommendation stands: each plugin stays its
own process and loads its own core modules, with plugins reaching each other
through the router.

## 11. How DEX maps onto this

| DEX (POC, Built) | This design |
|---|---|
| Envelope `{v, id, from, to, selector, args, replyTo}` | Adopted as the message shape |
| Router reads only `to` | Adopted |
| Dispatch table as data, edited by `define` | The cache directory (section 5) |
| `doesNotUnderstand` | Adopted; also the miss signal |
| Async send, local and remote indistinguishable | Adopted at the language boundary |
| Replayable log of state-changing messages | Unchanged; still the persistence model |
| Envelope version compiled into the bundle (E1) | `v` becomes a routing dimension |

**The TS–Rust seam (Direction).** The envelope is identical on both sides; only
the substrate differs. It is asynchronous at the language boundary, because a
handler may be remote, and synchronous inside the Rust process where the
substrate permits. The TypeScript kernel does not know whether a selector is
answered by JS, local Rust or a peer. This carries "identical cores on every
host" across the language seam rather than around it.

**Some dex calls are IPC (named, not designed).** A call from TS into Rust, or
across a process boundary, is IPC. The design accepts this and does not yet
minimise it. Where that seam sits, and what it costs, is Open.

**The DEX gaps carry over.** The four protocol gaps the POC found — input routing
with pass, capture and focus; per-invocation rather than per-field persistence;
no node lifecycle; an invisible painter cache — are properties of the contract,
not of TypeScript. They must be closed in the shared message model, or they will
reappear in Rust.

**E3 survives (Open).** A live-defined handler with full access to a node's
internals has no interface separating the two. If a live substrate is added,
`host/*` capability nodes must become enforced rather than conventional.

**Constraint C9 holds.** The POC server runs any Python a connected client sends,
guarded only by a token, and binds to localhost. A public display host must be a
separate display-only host.

## 12. Boundaries and non-goals

- **The design does not build a language.** Tony: "Building a new language from
  scratch is prolly not a good idea, unless can be derisked somehow." The
  prototype was Rust modules over a C ABI; DEX's dynamic language is TypeScript,
  which exists. The floor is the hard part whatever the language, so a new
  language buys nothing on the problem actually at hand.
- **DEX is orthogonal in content.** A container job or an external program does
  not care what the host is written in, so DEX contributes nothing to it. The
  market's floor is only "a work item is a message with a hash".
- **Fleet deployment is deferred** (C7). Focus is mizar and alcor; a transition
  state between nodes is acceptable.
- **Federation and the relay are out of scope** for this document.

## 13. What this design does not settle

In the order they block work:

1. **The loader.** Dynamic musl, a user-space loader, WASM, or subprocess.
   Gates whether in-process loading exists on a station at all (section 9).
2. **Version skew.** Whether the stale-while-revalidate window is acceptable, and
   what enforces a minimum version (section 7).
3. **The restart rule.** Contradicts one existing record; needs a decision
   (section 7).
4. **State across a swap.** Globals and thread-locals. `BEAM` `code_change` is
   the reference (section 7).
5. **The IPC seam** between TS and Rust, and across processes (section 11).
6. **The process model** — which binaries share a process (section 10).
7. **The service set** — what leaves the SDK and becomes a core service, and
   what happens when one is down.
8. **Purity declaration** — the mechanism by which a selector states its purity,
   and how that is checked rather than trusted.
9. **Verification for numerical work** (section 8).
10. **Score** and the acceptance rule (section 8, deferred).
11. **Compile trigger** — registration or first call (section 6).

## 14. First increment (recommended, not decided)

Given Tony's "a large change that I'd want to test and develop incrementally",
the smallest slice that tests the claims rather than the whole design:

Re-express the gateway's routing in the envelope shape, register one existing
plugin as a node, and measure two things: local call latency, and whether the
gateway binary changes when that plugin changes.

If latency stays in the nanosecond range and the gateway does not recompile, the
synthesis holds and the rest is porting. If either fails, the design changes
before more is built on it. One session's work.

Two prerequisites, both cheap and both on the critical path to everything else:

- split `awm-plugin-sdk` so a pin hashes a module's own inputs rather than
  `core/Cargo.toml` whole — this is the "makefile architecture" Tony asked for,
  and it is worth doing whether or not the rest of this design proceeds;
- decide the loader, because it is the one question that can invalidate the
  in-process seam the whole design rests on.

## 15. Provenance and known limits

Every claim labelled Built or Decided traces to the material the context document
was compiled from: journals on mizar and altair, the session transcript of
`awm/rust-build-strategy` (2026-11-08), the decision records, the end-state
records, the workspace docs (`ARCHITECTURE.md`, `FEDERATION.md`,
`docs/redux/goal.md`, `install.md`, `drafts/plugin-contract.md`), the DEX tree on
altair (`projects/dex/edge`, including `FINDINGS.md`), and the Claude Science
workbench. Paths, commit hashes and post ids are provenance for Tony; the reader
of this document set cannot open them.

Known limits, which apply to every claim in this document:

- Peer search reached alcor, which found no hit. capella's search was down.
  deneb, lyra and shogun were not queried.
- The science workbench listing returned 100 conversations, which may be a server
  cap.
- altair's journal was read from its SQLite store because its Rust `kb` was not
  serving during the search. A post that exists only in the newer store is
  missing from this set.
- The workbench scan matched keywords in the first 1,500 messages of each
  conversation.

Where the sources disagree — the restart trigger in section 7 is the live
example — this document says so rather than choosing silently. Where the sources
are silent, it says so rather than filling the gap from general knowledge of AWM.
Claims drawn from outside the set are marked as such in place: the `BEAM`,
`fidius`, `Subsecond`, `hot-lib-reloader`, `JASDA` and `Nix`/`sccache`/`distcc`
references are prior art for the reader's benefit and were not part of the
material this set was compiled from.
