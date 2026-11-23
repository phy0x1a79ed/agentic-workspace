---
name: outsource
description: Run heavy compute through the outsource market instead of locally — any job expected to take over about 10 minutes or 4 cores (builds, model fits, aligners, assemblers). Covers how to phrase a job, stage shared data, register data you moved yourself, and type materials with the shared vocabulary.
---

# outsource

## Purpose & Contents

This skill says when to send a job to the `outsource` tool and how to phrase it. Each verb's arguments and a worked example are in `outsource(verb="describe")`. Read that first. This file holds only the conventions the catalog cannot show.

## When

Send a job to `outsource` when you expect it to take over about 10 minutes or over 4 cores. Run anything smaller locally.

## Phrase the job

1. Give every path as an absolute path on your own node. The tool does not know your working directory. A call with `peer=<node>` reads every path on that node.
2. Pass each git repo as its own entry in `requires`. A repo is identified by its HEAD commit and its uncommitted changes.
3. Put the environment in `requires` as one metasmith-style `.env` file with a `container:` line. The protocol then runs inside that container.
4. Write `protocol` against base names. Each material lands in the work dir under its base name.
5. Write every output inside the work dir, never inside a material. A material is a shared copy that later jobs reuse, and the provider looks for `produces` only in the work dir. Point build output there, for example `cargo build --manifest-path release/src/x/Cargo.toml --target-dir $PWD/target`.
6. List in `produces` only the files you need back. A folder brings its files.
7. Set `resources` to the size the job needs, for example `{"cpus":2}`. A missing field takes its default. The provider meets it or declines.
8. Poll `status` with the promise id. A settled status carries the run stats.

## Shared reference data

- Type shared reference data, such as a database release, with `type` instead of a path. Every copy of a typed material has one id on every provider, so nobody moves it twice.
- Read `properties` before you type a material. Reuse an existing key. Add a key with `property_add` only when none fits, and give it a description.
- Run `prep` with `to` before a batch of jobs that need the same large material on another provider.
- Run a remote download as an `offer` with a typed output and `to`. The output stays at that provider for later offers.
- Register data you moved yourself, for example with Globus, with `import`. outsource moves data only with rsync.

## Many similar jobs

Shard a suite into one offer per shard instead of one long job.

1. Put the shared inputs (repo, dependencies, `.env`) in every offer's `requires`. They move once per provider and are reused.
2. Put a small kit folder in `requires` that holds one list file per shard and a runner script. Make the protocol `bash kit/run.sh <n>`.
3. Have the runner write its report (for example `junit.xml`) and its exit code into the work dir. Exit 0 when the shard ran, even if tests failed, so the report comes back.
4. Give each shard its own `dest`, and post all shards before you poll.

## Nextflow

The `nf-outsource` plugin (metasmith) runs a workflow's tasks as offers. Set `plugins { id 'nf-outsource@0.1.0' }`, `process.executor = 'outsource'` and `outsource { node = '<this node>' }`. With no hint the market places each task. Steer a process with `ext.outsource = [to: '<provider>']`, or a closure over the task to spread it.

## Results

- Re-posting an unchanged job returns the cached result. A file is identified by its path and modification time. A folder that is not a repo is identified by the names, sizes and modification times of everything under it.
- A very large plain folder makes every offer walk its tree. Type it, or make it a repo.
- Run `dispose` on large intermediate results when the work is done.
