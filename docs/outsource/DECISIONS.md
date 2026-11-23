# outsource: Decisions

One section per closed topic, in closing order.

## adoption (2026-11-22)

**Question.** Which work should go through baitan by default: any heavy agent command, only Nextflow/metasmith workflows, or both?

**Survey.** No agent has ever posted an offer (one test promise, 2026-11-12). Causes: no doc or skill names baitan; a hand-built offer with no example; nf-baitan unmerged. Altair metasmith sessions run ad-hoc R, Python and Apptainer work.

**User's words.** "I want this to be just general compute if possible."

**Decided.** baitan is general compute: any heavy command an agent runs is a candidate. The concrete call shape, how its dependencies move, and how cold nodes get prepared are the child topics `call-walkthrough` and `node-prep`.

**Landed.** This file.

## offer-shape (2026-11-22)

Child of `call-walkthrough`; absorbs `cache-default`.

**Question.** What does an agent's call look like, how do its dependencies move, and how is it identified without content hashing?

**User's words.** "an offer consists of \"materials\" (list of paths, some can be folders) and a \"method\" (a bash command)." "we still want to just model it off of Metasmith, but they stay distinct tools." "The environment should be just another material." "keyless actually works well." "We should get rid of props though." "we can just look for a .git folder and just track the ... commit hash ... we just accept the risk for normal folders." On a dirty repo: "a warning is fine. so we can false hit on a prior cache if changed files not comitted. This is acceptable... need to try for real".

**Decided.**
- The public offer is shaped like a metasmith transform declaration, but outsource and metasmith stay distinct tools, with no data-type registry:
  - `requires`: a keyless list of paths (files or folders) on the caller's node. Each lands in the work dir under its base name.
  - `produces`: a keyless list of paths in the work dir, copied back to the caller's node. No named output slots.
  - `protocol`: one bash command, run in the work dir.
  - `metrics`: what bids are scored on, default `[time]`.
  - `resources`: open, see `resources-model`.
- The environment is a material: a metasmith-style `.env` file (`container: docker://…`). The provider materialises it once, caches it under the file's id, and runs the protocol inside it.
- No props, at job or material level. Revised by `prep-verb`: a material may carry an optional `type`.
- Identity follows metasmith, never content: a file is `{path, mtime, origin}`; a plain folder uses its top-level stat and accepts the risk of a nested edit; a folder holding `.git` is its commit hash. A dirty repo keeps its commit-hash id and returns a warning; a false hit on uncommitted changes is accepted. Agent instructions ask for each repo as its own material. DVC pins need no rule.
- A result's id hashes the protocol and the material ids.
- Dependencies move as today: the winning provider rsyncs each material it lacks and prices the move into its bid; the plugin compiles the offer into the existing `task` plus `place` steps.

**Consequence.** Without props, one reference database at different paths on two providers has two ids. Prepping references on nodes moves to `node-prep`.

**Landed.** Build worker (see STACK).

## resources-model (2026-11-22)

Child of `call-walkthrough`, which closes with it.

**Question.** How should a job declare CPUs and memory, given that memory may grow with cores and not every job speeds up with more cores?

**User's words.** "I feel like the memory and the memory per CPU are runtime statistics. Those should be gathered to inform future choices, but I think fixed size today actually is the best outcome."

**Decided.**
- Keep `resources {cpus, mem_mb}` as a fixed size, as today. The provider meets it or declines.
- Peak memory and CPU use are runtime statistics. Each settle records them, for future choices. Learning from them stays deferred.

**Landed.** Stats recording goes with the deploy build.

## naming (2026-11-22)

The scope moved from `awm/baitan-tune` to `awm/outsource` (branch `feat/outsource`); these docs moved from `docs/baitan-tune` to `docs/outsource`. The session is `outsource-tune`.

## prep-verb (2026-11-22)

Child of `node-prep`.

**Question.** How do other nodes get prepared, so bids do not stay on the one provider that already holds the data (the greedy optimum)?

**User's words.** "I thought we could have prep be a biddable job itself." "we should have offers that ... can be explicitly given to a specific provider, and then a prep would just do the data transfer version of that offer. We don't want count. For the requires version, I think we want to use the type system so that different agent sessions would not duplicate the transfer or something like uniref50." "typed materials and to makes sense".

**Decided.**
- Prep is posted explicitly by the caller (an agent before a batch). The market does not prep on its own yet.
- Any offer may name `to: <provider>`. A directed offer still runs a one-provider bid round: the provider bids an ETA or declines with a reason.
- `prep {requires, to}` is the data-transfer form of a directed offer: no protocol, the provider stages the materials and pulls any `.env` image. No `count`.
- A material may carry a `type`, a property set that replaces its stat or commit identity, e.g. `{path, type: {db=uniref50, release=2024_01}}`. Every copy of it, on any provider and at any path, has one id, so a holder bids "done" and an in-flight transfer bids "done at T"; two sessions never move it twice. This revises `offer-shape`'s "no props" at the material level only.

**Landed.** Pending build, with `remote-fetch` and `globus`.

## remote-fetch (2026-11-22)

Child of `node-prep`.

**Question.** How does an offer download a typed material (uniref50) at the provider instead of moving it from the caller?

**User's words.** "a works. we need some controlled ontology for types or rather properties"

**Decided.**
- A download is an ordinary directed compute offer whose output is typed: `produces: [{path, type: {...}}]`, `to: <provider>`. The provider then holds that type.
- `requires` may name a material by `type` alone, with no path. The market fills its location from its holder records (the settles it already keeps for `dispose`).
- A material carrying its own `source` recipe, so bidders choose between copying from a holder and running the recipe, is deferred until real runs compare downloads with copies.
- Property names and values need a controlled vocabulary: topic `ontology`.

**Landed.** Pending build, with `prep-verb` and `globus`.

## globus (2026-11-22)

Child of `node-prep`, which closes with it.

**Question.** How does Globus fit in as a transfer method between clusters?

**User's words.** "Let's just not worry about Globus, but make it possible where agents can run Globus by themselves as an alternative. Then we can have outsourcing just use rsync by default. Well, I guess with no alternative. At least natively supported." On how an agent registers its own transfer: "a separate import verb, because prep is like do, whereas import is more like check."

**Decided.**
- outsource moves data with rsync only. Globus is not natively supported; agents may run it themselves.
- A new `import {to, materials: [{path, type?}]}` verb registers material that already sits at a provider. It goes straight to that provider, with no bid round (like `dispose`). The provider checks each path exists, records it under its type (or its stat or commit identity), and returns the ids; a missing path is an error. It reuses the provider's existing index-YAML importer. `prep` does; `import` checks.

**Landed.** Pending build, with `prep-verb` and `remote-fetch`.
