# Installing the `kb` plugin

## Purpose & Contents

This file covers what a node needs before kb runs, what kb writes, how a node gets papers, and how to roll the journal back. The code and its tests say how kb works.

kb is one static Rust process that serves the `journal`, `search` and `transcripts` tools.

## Install

kb is a core plugin. The deploy places its binary at `<AWM_DIR>/bin/kb`, and `run.sh` runs that binary. The gateway starts it. Do not start it by hand.

The deploy unpacks the model files from the artifact in `scripts/models.pin` to `<AWM_DIR>/models/all-MiniLM-L6-v2/<revision>/`. The revision and each file's sha256 are pinned in `core/crates/awm-embed/src/model.rs`. kb checks the hashes when it loads the model. Without valid files kb answers on keywords only, and it embeds the stored writes when the model loads.

| Env var | Default | Meaning |
|---|---|---|
| `AWM_SEARCH` | on | `0` turns embedding off |
| `AWM_EMBED_THREADS` | `4` | threads for embedding, capped at the CPU count |

## Data

| Path | Holds |
|---|---|
| `services/kb/lance/journals/` | every journal post since the cutover, which no rebuild can recreate |
| `services/journal/journal.db` | the frozen Python tables, and journal's other state |
| `services/search/search.db` | search's own store |
| `services/transcripts/transcripts.db` | transcripts' own store |
| `services/kb/lance/papers/` | the papers generations and `CURRENT`, about 28 GB each, on altair only |
| `services/kb/lance/<source>/` | the derived index of each other search source |

At its first start kb copies every post of the frozen tables into LanceDB. It refuses to serve until LanceDB holds each post and, with the model loaded, every post has its chunks. A marker file records the pass, so no later start copies again. Run `kb state --help` for the state check subcommands.

## Papers

A node serves the `papers` source only when `services/kb/lance/papers/CURRENT` names a generation. Today only altair holds one. Every other node lists no paper source, and its kb never loads bge-large.

A generation holds the Lance store, the bge-large files that encoded it, and the Zotero cursor. kb rereads `CURRENT` on each query, so a new generation goes live without a restart. The import keeps the current generation and the one before it, and deletes older ones.

Build a generation with `kb papers-import`. Run `kb papers-import --help` for its arguments. It needs:

- A semdb build (`projects/mining-microbiome/domain-models/data/semdb` on altair). The import reuses its vectors, so it embeds only the Zotero items semdb lacks.
- semdb's citation graph, `services/kb/graph/graph.db` on altair, for `cites` and `cited_by`.
- The bge-large-en-v1.5 files at the revision `core/crates/awm-embed/src/model.rs` pins.

**CAUTION** semdb is DVC-tracked. The import reads it in place and writes nothing beside it. Keep it that way.

The import needs no GPU and no sockeye. A full run on altair takes about an hour.

Each semdb domain is a collection under its slug from semdb's `domains.toml`. Choose the import by what changed:

- Run a full import (`--model`) after a domain is renamed or dropped. It builds a new generation from nothing.
- Run `--domains <slug>...` after a domain is added or rebuilt. It hardlinks the current generation into a new one and replaces only those domains' papers. A paper keeps every collection it already had, so a paper that leaves a domain keeps that label too.

**CAUTION** The clone hardlinks only Lance's `data/` and `_indices/` and the `model/` files, because nothing rewrites them in place. It copies every other file. A hardlinked file written in place would change the generation it came from, which is the rollback slot.

To roll back the papers source, write the previous generation's name into `CURRENT`. The import deletes the generation before that one, so check a new generation before importing again.

kb syncs the Zotero library into the served generation within seconds of a save. kb holds a long poll open on zotero's `items` (`wait`), which answers when a sync moves an item past the generation's cursor. The poll goes through zotero's singleton door, because the door carries calls but no event stream. A session stop is the backstop, at most once a day. A library item that matches a semdb paper adds `tony` to that paper's collections. Any other item becomes a new paper. Set `AWM_ZOTERO_PEER` to ask one node instead of the singleton.

## Roll back

1. Revert the code on the node.
2. Run `kb export-back` before the Python journal starts. It copies every post written since the cutover into the frozen tables.

**WARNING** Restore `.pre-rust/` only when `kb state check` proves damage. A restore discards every post written since the snapshot.
