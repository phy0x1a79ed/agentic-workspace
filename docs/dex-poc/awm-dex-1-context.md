# AWM + DEX: Context and Background

Compiled from the design conversation. Date, version and author are Tony's to set.

## 1. Purpose and scope of this document

This is the first of three documents. It records the objectives, the constraints
and the terminology that the next two depend on.

- **This document** — context and background: why the system exists, what problem
  the next stage addresses, what is fixed, and what the words mean.
- **Design** — the settled mechanism: the message model, the module and selector
  split, resolution and fill, the build market, the swap. Written next, on Tony's
  prompt.
- **Open questions** — what is not decided, each with its options and the
  consequence of each. Written after the design.

Nothing in this document should be read as a design decision. Where a design
matter is named here, it is named only because the design doc has to answer it.

Status labels, used only where the distinction matters:

- **Built** — runs in the code today.
- **Decided** — Tony settled it; not built or not deployed.
- **Direction** — Tony stated the intent; the design is open.
- **Open** — nobody decided.

## 2. Background

### 2.1 What AWM is for

AWM is Tony's infrastructure for working with agents across machines. Its stated
end is leverage: "the fundamental goal here is using awm to leverage what I can
do." It reduces Tony as the bottleneck, organizes work linearly to the main
operator, and accumulates wisdom so that agentic, deterministic and human effort
is not wasted.

The current shape is the Rust redux: one Rust binary (`awm`) acting as bootstrap,
CLI, MCP proxy and gateway with a plugin loader; separately supervised plugins;
each plugin publishing tools with verbs; each plugin owning its own state and
referring to others by natural key through the gateway. Hot swap at run time
works: a plugin restarts without disturbing the gateway, and the gateway restarts
without disturbing the plugins.

### 2.2 Why Rust, and what it cost

The redux replaced a Python core. The reason was resource cost, not taste:
stations — small nodes that run services for the rest of the network, typically
2 cores / 8 GB or 4 cores / 16 GB — could not afford Python. The port bought the
resource profile back.

It also removed every perk of a dynamic language. Two costs followed:

- **Build time.** Rust builds took hours. The measured cold no-LTO build of
  everything on capella is 8 min 5 s across 682 steps; the complaint is that the
  cost was paid repeatedly, "sometimes multiple times in the same session."
- **Recompile on unrelated change.** Plugins link four shared crates
  (`awm-plugin-sdk`, `awm-proto`, `awm-trust`, `awm-statecheck`), so a one-line
  SDK change rebuilds nearly every plugin. Thin LTO made that about 45 minutes.
  The SDK is 3,665 lines and mixes the wire client with privacy, telemetry,
  sessions and supervision. Whole-file hashing of `core/Cargo.toml` in every pin
  turned a manifest touch into a fleet-wide rebuild.

The contradiction is the point: the plugin system is already modular at run time,
and still forces a recompile at compile time.

### 2.3 The DEX link

DEX is a separate project with the same root idea: a visual, programmable
workspace built on "develop the system in the system." Its October redesign
pushed toward one message abstraction — "a message is a message" — identical
cores on every host, and contracts that change live. A TypeScript proof of
concept proved the idea and located the protocol gaps. In November Tony pointed
at DEX as the display layer agents connect to, beginning with a navigator over
cyanoverse outputs.

The link Tony made: if the redux removed the dynamic perks and the build cost is
the daily tax, can something better be designed — one that keeps the resource
profile and regains the change properties — and can DEX's model be the basis of
it.

### 2.4 Prior attempts and what they taught

Recorded because the design doc will lean on these lessons and should not
rediscover them.

**`remote-ui`, September.** A headless server sent draw shapes to a dumb browser
viewer, and was rejected as "too laggy to use" — hover and drag at 60 to 120 ms,
because the client held no state it could interpret. The lesson: the cut belongs
at the message layer, with the node tree and the draw on the client. The same
port found that `usize` is not portable to wasm32 and silently dropped text
frames — a portability trap for any identical-core-on-every-host goal.

**The DEX TypeScript POC.** Proved the message model: full GTDB r232 tree of
379,601 nodes at 1.2 to 1.3 ms p95 per frame, no network during interaction, and
a warm Python round trip of about 3 ms from the browser. It also found four
protocol gaps that each surfaced independently in three or four packages: input
routing needs pass and capture plus focus; persistence is per handler invocation
rather than per field; nodes have no lifecycle and `kill` gives no notice; and
the painter's cache is invisible to nodes. It established that the unit of
messaging must be chosen by cost — "a view, not a datum" — and that silent reply
loss was the most expensive bug class found. Critically for the design, it showed
that a new node type and a new selector work at run time, but a new envelope
contract does not, because the envelope version was compiled into the bundle.

**The throwaway `dlopen` prototype, 2026-11-08.** Outside the repo, nothing
committed. Rust with `dlopen` and the C ABI: a module exports
`awm_send(selector, bytes, out buffer)` and `awm_free`; the host holds an atomic
pointer to a function table. Call cost including copy in, allocation, copy out
and free was 76 ns. 5.8M calls across three swaps under constant load from
another thread, none failed. Panics were contained by `catch_unwind`. A static
musl host refused with "Dynamic loading not supported". Reloading from the same
path silently kept the old code, so each version needs a new filename. Untested
at that time: dynamic glibc or musl, async completion, state handover, and the
loss of compiler checking at the seam.

**The build findings, 2026-11-08.** Eight land batches rebuilt nine or more
binaries in under nine hours; five of them moved `core/Cargo.toml`, which every
pin hashed whole. Cargo resolves features across the packages in a build, so a
changing build set misses the cache: 665 external units built together against
972 distinct across 18 isolated builds. Per-worktree target directories reached
549 GB.

### 2.5 Fleet context

mizar is home base and holds the canonical repo and build container. alcor hosts
singletons and runs the live services. capella is the large machine (16 cores,
68 GB) and is the natural builder. lyra has 16 cores and 7 GB. deneb hosts the
public relay and is deferred. shogun is in the fleet. altair holds the science
work, including DEX and cyanoverse, and does no AWM work — it only receives each
release. Each node owns its own journal and scopes databases; agents reach across
with a peer argument.

## 3. Objectives

Stated as properties the end state must have. These are the drivers; the
mechanism that satisfies them is the design doc's business.

**O1. Resource profile.** AWM runs on stations: 2/8 and 4/16 machines, and
portable enough to run anywhere, including a Raspberry Pi. This is why Rust was
chosen and it does not get traded away.

**O2. Compile cost is local.** A change costs its own compilation unit and no
other. A dynamic dev change does not force a rebuild of the chain from
dependencies through the SDK to the core. Tony: "the compile time is really the
issue here."

**O3. Isolated builds that serve a contract.** Modules are isolated by
construction rather than by convention. A dependency such as LanceDB is its own
module behind its own contract, so a change to a plugin does not rebuild it.

**O4. Change without restart.** Behaviour changes reach a running system. The
run-time process swap already satisfies part of this; in-process replacement is
the harder part and is not yet justified on its own.

**O5. Distributed work allocation.** Work is allocated across the fleet by a
market: sessions post work, nodes bid, work is distributed by capability. There
is no central scheduler. Nodes are cooperative, so deviations from
reproducibility are bugs rather than trust failures.

**O6. One message model across both projects.** AWM and DEX share a message
model, so that DEX ideas can inform the AWM core and DEX can call into it. The
strongest reading — one shared kernel — is not stated by anyone and is not
assumed here.

**O7. No new language.** Building a language from scratch is out unless it is
derisked. "Built on established tools" is the constraint.

## 4. Constraints and givens

- **C1. Portability without glibc coupling.** musl is wanted "so that we dont
  have to deal with glibc version mismatches and so that awm can work anywhere,
  like a raspberry Pi." The reason is portability, not only binary size. Any
  mechanism that reintroduces a libc version dependency works against this.
- **C2. Cooperative network.** Nodes are expected to cooperate. Reproducibility
  is therefore a verification property: an output hash either matches or it is a
  bug. Trust rules exist, but the design does not lean on adversaries.
- **C3. No central scheduler.** Allocation is decentralized. A session may run
  an auction for its own work; nothing coordinates the fleet globally.
- **C4. Small nodes do not compile.** A 2/8 station should not run a Rust
  compiler. Compilation is a distributed service; loading is local.
- **C5. In-process call overhead matters.** The seam between caller and callee
  should be a function call where possible, not IPC, because the overhead is the
  thing being optimized.
- **C6. Heterogeneous capability.** Nodes differ in CPU, memory, GPU and
  ABI baseline. A work unit must be allocatable on capability, not on task type.
- **C7. Deferred.** Fleet deployment, including deneb. Focus is mizar and alcor.
  A transition state between nodes is acceptable.
- **C8. The DEX edge branch is local only.** `feat/edge` on altair must never be
  pushed there. Whether it is shared with Sebastian is Tony's call. Any work that
  touches DEX carries this constraint.
- **C9. The POC server is not deployable as a relay.** It runs any Python a
  connected client sends, guarded only by a token, and binds to localhost. A
  public display host must be a separate display-only host.

## 5. Terminology

- **Node** — a machine in the AWM network.
- **Station** — a node that runs services for the rest of the network.
  Typically 2/8 or 4/16. Small, numerous, and the reason for the resource
  constraint.
- **Session** — an agent session running in a checked-out repo worktree.
- **Scope** — the worktree a session runs in.
- **Module** — a compilation and loading unit. Hashed, compiled for a target,
  loaded and swapped as a unit.
- **Selector** — a named function within a module. The fine-grained unit of a
  call and, in the emerging design, of purity.
- **Dex call** — Tony's term for a call into the shared message model: a
  collection of pure functions with dynamic fill options, as distinct from
  ordinary code inside a module.
- **Fill** — acquiring something absent from a cheaper location by paying a
  higher cost elsewhere, and writing it back so the next request is cheap.
- **Market** — the decentralized allocation of work across nodes by bid.

## 6. The framing

Three lines meet, and they meet at a schema rather than at an implementation:

- **AWM** — compile cost is the driver. It wants a frozen boundary small enough
  that a change does not cross it.
- **DEX** — live change is the driver. It wants a contract that can move at run
  time.
- **The market** — heterogeneous allocation is the driver. It wants work items
  identified by content and matched by capability.

What they share is a discipline, not a system: the frozen boundary stays small,
identity is a hash of content, and nothing above the boundary is scheduled
globally. The design doc takes that discipline and states the mechanism.

## 7. Carried forward to the design document

Named here because the design doc must resolve them, not because they are
settled:

- **Compile placement.** Compilation is a distributed service; loading is
  in-process on the target (Direction). The trigger point — registration or
  first call — is not settled.
- **The loader on musl.** Static musl cannot `dlopen`. The options are a
  dynamically-linked musl host, a user-space loader, WASM modules, or a
  subprocess seam. C1 constrains the choice; the choice is Open.
- **The purity split.** "Code within a module is code, but dex code is really a
  collection of pure functions with dynamic fill options" (Direction). Where
  exactly the seam sits between the two is the design doc's central question.
- **The IPC seam between TS and Rust.** Some dex calls will be IPC. This is
  named and not yet designed.
- **The swap.** Whether a swap is a dex call; whether it can be made pure;
  whether publication and activation are separate operations.
- **Version skew.** During the window between publication and activation, some
  calls get the old answer. Whether that is acceptable, and what carries the
  version, is Open.
- **The score.** Which source a node chooses to fill from, and who sets it. Tony
  deferred this on 2026-11-08: the market bidding discussion comes later.
- **State across a swap.** Globals and thread-locals are the known gap. Neither
  system has an image.
- **The service set.** What leaves the SDK and becomes a live service, and what
  happens when one is down.
- **The first increment.** The smallest testable slice, and its order against
  the redux landing.
- **What triggers a restart.** The sources disagree. The end-state record and
  the name-table section of the plugin contract say the landing agent names what
  restarts, even if that disagrees with the file changes. The contract's runtime
  section and the build findings say a plugin restarts only when its bytes
  changed. This is a live inconsistency in the built system, not only an open
  design question, and the design doc should state which rule it adopts.

## 8. What this document does not cover

- The mechanism. No hierarchy, no module/selector mechanics, no market protocol.
- The decision record. Each open question with its options and consequences
  belongs to the third document.
- Provenance. Paths, commit hashes and post ids are for Tony; the reader of this
  document set cannot open them. Where this set is silent on a point, it says
  so rather than filling the gap from general knowledge.