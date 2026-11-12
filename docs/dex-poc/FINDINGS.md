# DEX POC findings

Code: `poc/` (standalone workspace: `dex` lib + `node` binary, `modl` demo cdylib), deleted by the baitan build. Read it and the raw outputs `poc/results/e{1..4}.out` at commit 48c840ab.
Setup: mizar (glibc 2.39) is the caller node, capella (glibc 2.35, builder) is the second node, reached over `ssh` stdio with no listening port. Modules were built on capella and loaded on mizar. Nothing touched the production gateways except read-only `awm status` and `scope search` calls for the baseline.

## E1 calling mechanism

One call site (`Router::call(from, to, selector, a, b, deadline)`); placement lives only in the router table.

| Placement | Format | p50 | p99 |
|---|---|---|---|
| in-process `cdylib` | struct | 0.2 µs | 0.3 µs |
| separate process, Unix socket | strict JSON | 41.6 µs | 240 µs |
| capella over ssh stdio | strict JSON | 728 µs | 43 ms |
| existing gateway `POST /invoke` (`awm status`, local) | JSON/HTTP | 5.0 ms | 13 ms |
| same with `peer=capella` | JSON/HTTP + ssh edge | 8.4 ms | 17 ms |

- The same code path returned identical results and the same closed errors for all three placements: `doesNotUnderstand` (bad selector, unknown name), `timeout` (remote only, see below), `unavailable` after the callee was killed (0.0–0.1 ms, no hang), and `moved` followed by one re-resolve and retry (stub callee).
- A strict envelope with an unknown key is refused.
- The struct path is about 100x the bare 2.2 ns pointer call. The router's `RwLock`, string selector lookup and envelope construction cost the difference. It is still 200x cheaper than the next placement.
- The ssh carrier has a bad tail (43 ms p99); the median is fine. The cause was not investigated (candidates: ssh channel buffering, the reader thread hand-off).
- The gateway baseline is 100x slower than the unix path locally and 10x slower than ssh. The baseline opens a new TCP connection per request, so part of that is connection cost, not routing.

Contradicted or missing:
- **A deadline does not bound an in-process call.** `sleep 300ms` with a 50 ms deadline returned `Ok` locally and `timeout` remotely. A synchronous local call cannot be preempted; the deadline is only enforceable at a seam. Either local calls run on a worker with abandonment, or the contract says the deadline is advisory for local callees.
- Callers used a blocking call, not a future. The "ready on first poll, no allocation" claim is not tested.

## E2 update flow

Flow: capella origin publishes `L -> digest`. The mizar node holds a subscription over the ssh link. A `publish` event arrives, the node fetches the blob by digest over the same link, verifies sha256, writes `<digest>.so`, `dlopen`s it, checks the ABI and runs `dex_init`, then swaps an atomic pointer.

- 4 threads called `L.calc` in a loop through the swaps (about 7.2 M calls total): **0 errors**, and each thread saw outputs `[3, 1002]` once, in order (v1 to v2, never back).
- Event to live: about 52 ms (fetch, verify, load, swap), 90 ms on first v2 in an earlier run.
- A tampered blob (digest mismatch) was rejected, and so was a build whose `dex_init` panics. v2 kept serving (`calc = 1002`).
- Offline catch-up: with the link down, capella published a new version. On reconnect the node sent its held digest and received `current` with the new digest, then swapped. A second reconnect with nothing new got `notmodified`.
- State is lost across the swap: the module's call counter went from about 1.5 M to restart-from-zero.
- Answers: the update originates at the origin node, the new bytes come from the origin by digest (a binary, built by the origin or a builder and then published), and the subscriber is pushed only the name, digest and sequence.

Contradicted:
- **`catch_unwind` on the host does not contain a module panic.** A panic unwinding out of the cdylib aborted the whole host (`Rust cannot catch foreign exceptions`). Containment has to be inside the module: `dex_init` and `dex_call` catch their own panics and return a status code (done in `modl`). The ABI must carry a status, and a module author who forgets `catch_unwind` takes the host down. Segfaults were not tested and cannot be contained in-process.
- A rejected init-panic blob stays in the node's store; the loader should delete a blob it rejects.
- Old modules stay mapped (leaked) so in-flight calls remain valid; the cost per swap is not measured.

## E3 cache and page table

- Page table: one `table.json` per node, `name -> Copy{digest} | Service{route}`. The origin's tag table is authoritative. The node table changes only on origin events (`current`, `publish`), never by time.
- Store capped at 3 blobs over 5 names: 3 evictions. The pinned module (n1, loaded) was never evicted, and neither was the `Service` entry. An evicted copy (n2) was refilled on its next call.
- Levels measured: L0 loaded 0.00 ms, L1 store hit plus `dlopen` 6.7 ms, L3 remote fill 60–130 ms (hex over ssh, includes load).
- An origin `publish` event on n3 updated the table entry; the next call refilled (L3) and the superseded blob was evicted first (orphan before referenced).
- Restart: the table and store survive (6 entries, 3 blobs); loaded and pinned modules do not. Re-subscribing with held digests returned `notmodified` for all five names, so a restart costs five round trips and no fetch.
- A service is a route only: the store never holds it, and no eviction path touches it.

Caveat: the five "modules" are one `.so` with different trailing bytes, so the content addressing is real but the code is the same.

## E4 market build

- mizar and capella both received a work item `{src_digest, features, glibc_max 2.35, triple}`. mizar **declined** (`no cargo`); capella **bid** (glibc 2.35, estimated 60 s default). The bid round took 421 ms. mizar awarded to capella.
- capella built in 107 ms (a tiny crate with no dependencies), returned the digest, and mizar fetched by digest, verified, loaded and called it (`calc(3,4) = 1012`, version 2).
- **Reproducibility:** five builds were byte-identical (same digest `c3209405…`): same dir, a different build dir, and both with `--remap-path-prefix`. The digest also equals the E2 v2 build made in another directory. This holds for a dependency-free cdylib built with one toolchain; a crate with registry dependencies or absolute paths may differ and was not tested.
- Source travels inline in the award message (hex, verified against `src_digest`) rather than being pulled by the builder. The "builder asks for an input by digest" event is not built.

## What the POC did not test

- Layout-version mismatch falling back to JSON and upcasters.
- Effectful-call dedup by `id`.
- More than one bidder, contention, and a bid that loses.
- alcor as a third node; a live service across a `moved` (only a stub).
- Segfault and runaway-module containment.
- The Phase B adapter into the real gateway.
- Production loader choice (glibc dynamic vs static musl).

## Design deltas to carry into the build

1. Module panic containment is the module's job; the ABI needs a status and the SDK should wrap entry points.
2. Deadlines are enforceable only across a seam; decide the local rule.
3. The loader must delete rejected blobs and write to a temp name before the digest check passes.
4. The router overhead (~200 ns) is worth a fast handle (resolve once) before anything claims "ns-level".
5. Investigate the ssh carrier tail latency before choosing the carrier.
