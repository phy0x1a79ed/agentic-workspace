# baitan wire flow and job kinds

## Purpose & Contents

This file is the contract between the baitan market, providers, fronts and callers that the types in `core/crates/baitan-proto` cannot express alone: the order of messages, who answers what, and the `spec` and slots of each job kind. Design reasons are in `DECISIONS.md`.

## Message flow

Every message is an `Envelope` from `baitan-proto`. `to` names a provider id or a caller id. Each `ask` gets exactly one reply: `return`, `raise` or `doesNotUnderstand`. A `send` gets no reply. The market replies to an unknown promise with `raise UnknownPromise`.

1. A provider link sends one `register` ask per provider it hosts. The market returns `null`.
2. A caller sends an `offer` ask with `{set}`. The market returns `{"promise": <id>}` at once.
3. The market sends `offer` with `{set, promise}` to each eligible provider. A provider is eligible when its link is up, its version range holds the offer envelope's `v`, and the set's `place` is a subset of its registered `props`.
4. Each provider answers with a `bid` ask. `values: null` is a decline with a `reason`. The market returns `null`.
5. The round closes `window_ms` after the offers go out, or earlier when every eligible provider answered. With no bid that carries values, the promise fails with `NoBids`.
6. The market sends `award` with `{promise, set}` to the winner. It sends `event` with phase `awarded` to every other bidder, so they release the capacity they priced in.
7. The winner sends a `started` ask, then a `settle` ask. `kept` settles the promise. `broken` starts a fresh round without every provider that broke this promise. A fresh round with no bids fails the promise with `NoBids`.
8. A caller's `status` on an awarded or started promise makes the market ask the provider `status` with `{promise}`. The provider returns a `PromiseStatus` as it sees it, with `life` from its backend. The market applies a settled or failed answer as if a `settle` had arrived. It returns its own `PromiseStatus` with the provider's `life`. A provider whose link is down gives `life: "unavailable"`.
9. A caller's `cancel` before award fails the promise with `Cancelled` and awards nothing. After award, the market sends `cancel` to the provider and fails the promise with `Cancelled`. The provider kills the job and sends nothing more for it.
10. A caller's `dispose` with `{ids, holder?}` groups the ids by holder: `holder` when given, else the provider whose settle named the id. The market asks each holder `dispose` with `{ids}` and returns once all holders return. An id with no known holder counts as done.
11. A caller's `review` on a settled promise is sent on to the provider that settled it. The market also records the per-axis error for that provider and contract.
12. A caller's `watch` makes the market send `event` with the `PromiseStatus` to that link on every phase change of the named promises, or of all promises.
13. When a link drops, its providers become ineligible. Their awarded promises stay awarded. When a provider id registers again, the market asks it `status` for each open promise awarded to it and applies each answer. `raise UnknownPromise` counts as a broken promise.

The contract of a set, for history and corrections, is the `contract` prop of its first compute step, else that step's `op`.

## Identity

- A compute step's slot id is `slot_id(step.key(), slot)`. The requester can compute it before the award.
- A provider indexes each result under its expanded set: the step's `props`, the pair `op=<op>`, one pair `input=<id>` per input, and any dependency versions it used. A request hits when its own pairs (the same construction without the provider's extras) are a subset of a stored entry's pairs. A hit settles with the stored instance ids, not the request's slot ids.
- An imported instance's id is `instance_id(props)` of the property set its index YAML declares.
- A leaf file that no baitan step produced has the id `instance_id({path, origin})`, where `origin` names the provider that holds it.

## Job kinds

A step's `op` picks the kind. The provider writes each kind as a `run.sh` in a fresh work dir and hands it to its backend.

| op | spec | slots | result |
|---|---|---|---|
| `task` | `{script, stage, outputs}` | `["out"]` | `out` is the work dir after the script exits 0. Its listing holds the relative paths that match `outputs` |
| `cargo` | `{git, rev, bin, target?, features?}` | `["bin"]` | `bin` is the built binary file |
| `place` | `{path, src?}` | none | each input id is copied into `path` at the provider `to`, under the base name of its source |

- `task.script` is the shell body to run. `task.stage` maps a name in the work dir to `{id, path?, src?}`. `path` selects a file inside a directory instance. `task.outputs` is a list of globs.
- `src` (per stage item, or `place.src` keyed by id) is the absolute path at the input's place. Give it only when the input's `start` fact names no `path`.
- A settled `at` fact names the holder's path in `o.path`. Copy settled facts into a later set's `start`, and the next provider fetches without `src`.
- A provider runs `place` on its own host, never through its backend. A Slurm front therefore moves data from the login node and queues nothing for it.
- A provider without cargo declines `cargo`. A placement-only provider declines every set that holds a non-placement step.
- A placement step's inputs are slot ids of earlier steps in the same set, or ids the provider holds. A provider resolves a slot id that a cache hit replaced to the stored id.
- A step whose input the provider does not hold is fetched from the nearest place in the set's `start` facts. The fetch time goes into the bid.
