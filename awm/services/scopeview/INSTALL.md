# scopeview

scopeview shows what a scope holds: the raw tables a metasmith pipeline publishes under `data/`, and the chain of work built on them. The chain runs question → figure → interpretation → point → prose. The page shows any git ref of the scope, and its chat reaches an agent seated in the scope.

## Purpose & Contents

This file holds what the code cannot show:
- why the chain is stored as it is
- which contracts the service, the page and a study's scripts must share
- how a study runs as versions
- how the chat finds its agent
- where the plugin is placed
- how to verify it

The verbs and their arguments come from `awm scopeview describe`. The chain format is stated in the module docstrings of `awm/scopeview/chain.py` and `awm/scopeview/trace.py`.

## Why the chain is files

Each chain node is one markdown file. The edges are declared on the child, as `from`. Nothing stores the graph, and every read rebuilds it. A node keeps `seen`: the hash of each input at its last review. A node whose input hash moved is stale, so staleness needs no event and survives any merge.

The chain is not a metasmith data instance library. A library's ids are a path plus a modification time, and its single index file conflicts on every merge. Text hashes as a git blob, and data hashes as its DVC md5, so nothing is hashed twice.

## Contracts that two sides must keep

- **Citations.** A body cites as `[[id]]`, `[[id#n]]` (the nth top-level list item) or `[[id?conditions]]` (table rows). `trace.py` parses them. `pages/scopeview/src/lib/markdown.ts` parses them again to bind each sentence to its citations, and draws none of them. Change both together.
- **Figure marks.** A figure's front matter `rows: {table, key}` names its table and key column. Its `made_by` script sets each mark's `customdata[0]` to the key, or to the list of keys of an aggregated mark. Later columns are free for hover text. `trace.mark_keys` and `trace.ts` `keyValues` both read this rule. A script that breaks it traces silently to the wrong rows.
- **Key values.** A key that holds `,` or `&` cannot be cited, because both are selection delimiters.

## A study runs as versions

The study scope's branch is the published study. Every edit comes from a nested scope that merges back into it, so a version is a merge commit.

1. Make the edit in a nested scope such as `<study>/<topic>`.
2. Run `awm scopeview check --gate` before the merge. The study's `pre-commit` hook runs the same check, and `pre-merge-commit` links to it. The hook fails closed when scopeview is down.
3. Merge with `git merge --no-ff` in the study worktree.

- **WARNING** `scope complete --merge` merges into the project's default branch, not the study branch. Do not use it for a study edit.
- **CAUTION** Two branches that regenerate one figure conflict on its `.dvc` pin, and a pin cannot be merged by hand. Take the upstream pin, rerun the node's `made_by`, and `dvc add` again.

A stale node with no `stale_ok` is always a problem. Only the gate counts it against `ok`, so the page shows it in amber and the gate refuses it.

An old ref reads text with git and data by the md5 its pins name, straight from the shared DVC cache. scopeview never runs `dvc`. An object missing from the cache is reported, not fatal.

## The chat

The chat is a room seat (`docs/redux/sessions.md` R149 item 7). The `chat` and `say` verbs call `seats room` on the scope's node. That call declares the seat the first time and returns the room's link. scopeview is a guest over the relay's HTTPS API, so the relay keeps the messages and scopeview keeps only the link.

## Placement

The plugin runs on mizar, where Tony opens the page, and on altair, where the study scopes live. The page reaches another node's scopes with `peer`, because the service reads only its own node's scopes. The cloud project's `homelab/inventory/group_vars/all/plugins.yml` places it under `awm_plugins_homed`. Change it there, not in a node's `config.toml`.

## Install

Run `install.sh`. It installs the service into the awm env. The service needs metasmith's `metasmith.models` for the provenance DAG, and it imports nothing else from metasmith.

## Verify

1. Run `awm scopeview check --project cyanoverse --scope engcyc-visualizer --gate --peer altair`. Expect `"ok": true`.
2. Run `awm scopeview trace` on a figure selection, such as `f-mag-quality?gtdb_phylum=Bacteroidota`. Expect rows with a `total`.
3. Open https://mizar.tail080e54.ts.net/p/scopeview/scopeview/. Pick altair, then a study scope. Turn trace on, and hover a sentence to see its rows in the info panel.
