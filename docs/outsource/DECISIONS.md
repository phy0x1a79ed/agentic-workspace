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

## ontology (2026-11-22)

**Question.** Where does the controlled vocabulary for material properties live, and how strict is it?

**User's words.** "we need some controlled ontology for types or rather properties" / "a"

**Decided.**
- The market holds the vocabulary: each property key has a description and an optional list of allowed values.
- An offer, prep or import with an unknown key, or a value outside a key's list, is rejected; the error names the closest known keys.
- `property_add(name, description, values?)` adds a key at runtime. The vocabulary is outsource's own (standalone of awm), modelled on awm's shared journal-tag vocabulary.
- Values are case-folded to lowercase before identity.

**Landed.** Build after the prep worker (both touch outsource-proto and the market).

## discovery (2026-11-22)

**Question.** What tells agents to use outsource, and when?

**User's words.** "brief section, maybe 1 sentence in workspace.md? where the other awm protocols live? + an outsource skill / not c"

**Decided.**
- One sentence in the workspace `AGENTS.md`, in the MCP tools list of tools that change how you work: use `outsource` for heavy compute (over about 10 minutes or 4 cores) and load the `outsource` skill.
- An `outsource` skill holds the how: absolute paths, each repo as its own material, the `.env` material, typed materials and the vocabulary, `prep` before a batch, `import` after a self-run transfer, `to`, and worked examples.
- No hook.

**Landed.** Written at the end, with the deploy, once the verbs settle. The skill must reach every node agents run on (altair included).

## topology — 2026-11-23

**Question.** Where do the market and the providers live, so that an agent on any node can outsource work?

**User's words.** "it is really 1 market, and each node has an entry and supports/runs its own providers. the edges dictate which nodes can offer to which providers"

**Design.**
- Every agent host runs the outsource service. The service is that node's entry to the one market and the host of that node's providers: its own provider, plus any Slurm cluster providers it fronts (altair: fir, sockeye).
- Each node runs one provider process that owns its state. Every entry that reaches the node attaches to it over a socket. Entries never spawn providers.
- An agent posts at its own node's entry. The entry offers to the providers of its own node and of every node its awm edges reach. Data moves along those same edges.
- Shared market state is the same at every entry. Holdings and caches live at the providers. The vocabulary syncs through the providers by union.
- Replaces the single mizar market that started providers over ssh (dex-poc).

**Landed.** Release 4 dfa5a2b9, live 2026-11-23. An altair-entry offer ran on fir and returned. Placement: outsource joined the `core` package in cloud `homelab/inventory/group_vars/all/plugins.yml` (cloud dev a8455a5). The entry's reach comes from the user edges in `edges.yml`. A call with `peer=<node>` reads its paths on that node.

## capella-provider — 2026-11-23

**Question.** Should capella stay a provider when it is awake?

**User's words.** "capella is an opportunistic provider, yes"

**Design.** capella runs an entry and a provider like any agent host. It bids only while it is awake. Nothing wakes it for a job. It gets the service from the `core` package the next time its awm config is applied.

**Landed.** No code. Cloud dev a8455a5 places it.

## slurm-vanilla — 2026-11-23

**Question.** How should the Slurm provider batch and pace its cluster calls, and treat whole-node clusters (dex-poc `arrays`)?

**User's words.** "I think a provider that optimizes against slurm should be pretty vanilla. Nothing fancy here." "lets go to 10s delay for ssh calls per cluster -> should still aim to be much lower, ie when recieving 1000s of jobs, do not submit 1 at a time" "5 should be a soft limit -> it misses bids on efficiency rather than straight reject. use the market system"

**Design.**
- At most one ssh call per cluster per 10 s (was 60 s). metasmith's Slurm preset polls every 10 s and submits at 1 per 5 s.
- Each call does everything due: cancels, one `sbatch` array per (cpus, mem, walltime) group, the queue and exit-file listing, and `sshare` when stale. A failed `sbatch` fails only its own jobs, with Slurm's error text.
- No throttle of our own. Slurm's limits apply, and the queue-wait bid grows with our own pending jobs.
- Walltime is about 2× the job's estimate, capped at the cluster setting.
- Whole-node clusters stay open to every job. Each bid carries a `waste` axis (share of allocated cores left idle), and the default axes score it, so waste loses bids instead of being refused. Packing is out.

**Landed.** Release 5.1 98c26809 (2026-11-23). T14 (releases 5.3 8fa50dde, 5.4 64f6f4ec) added: a Slurm task writes `<workdir>/log` like a local one, a task's image is pulled on the login node before the job (trillium's compute nodes cannot reach a registry), and a directed round waits up to 300 s for its provider. Live: trillium, nibi, fir and sockeye served one Nextflow run together.

## waste-axis — 2026-11-23

**Question.** How does the market keep small jobs off whole-node clusters without refusing them?

**User's words.** "5 should be a soft limit -> it misses bids on efficiency rather than straight reject. use the market system"

**Design.**
- Every bid carries `waste` = 1 − requested cpus ÷ allocated cpus. It is 0 for a provider that allocates what was asked. A cluster with `whole_node = true` allocates `node_cpus`.
- The default axes are `time` (weight 1) and `waste` (weight 0.5, lower is better). An offer that names its own `metrics` keeps them.
- Open: the robust-z picker scales each axis by the bids' spread, so among slow HPC bids a small time lead still beats a near-total waste. Tony was paged with three options (score time as a ratio to the best bid, raise the waste weight, or decline waste above 0.9 unless directed).

**Landed.** Release 5.1 98c26809. In T11, trillium bid waste 0.99 on 2-cpu shards and lost to altair.

## nextflow-executor — 2026-11-23

**Question.** Can one Nextflow workflow run its tasks across several HPCs and altair at once through the market?

**User's words.** "Can we also POC an outsource executor for nextflow? -> to prove that nextflow can use the market system and be served by multiple HPCs + dedicate compute nodes like altair simulatneously for a single workflow?"

**Design.**
- The `nf-outsource` plugin lives in metasmith (`feat/engine/outsource-executor`, ported from nf-baitan). It sets `process.executor = 'outsource'` and talks to the node's entry through `outsource connect`.
- Each task becomes one offer. Its inputs go as materials, and its outputs come back into the task work dir, so no shared filesystem is needed.
- `ext.outsource = [to: <provider>]`, or a closure over the task, steers a process. With no hint the market picks.
- The plugin builds the offer set itself, because the entry accepts only `{set}`. That copy of the client compile can drift silently (stacked).

**Landed.** metasmith ba2190aa, d513332e (unpushed). T12 on release 5.4: unhinted, 20 tasks spread over altair, nibi and fir in 1 min 2 s. Hinted over five providers, 4 tasks each on altair, fir, nibi, sockeye and trillium in about 1 min.
