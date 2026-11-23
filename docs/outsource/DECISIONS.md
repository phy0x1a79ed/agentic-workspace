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
- No props, at job or material level.
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
