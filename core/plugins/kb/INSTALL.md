# Installing the `kb` plugin

## Purpose & Contents

This file covers what a node needs before kb runs, what kb writes, and how to roll its journal back. The code and its tests say how kb works.

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
| `services/kb/lance/<source>/` | the derived index of each other search source |

**CAUTION** `services/kb/` also holds the Python papers plugin's state, up to 170 GB on altair. Do not treat that folder as kb's alone.

At its first start kb copies every post of the frozen tables into LanceDB. It refuses to serve until LanceDB holds each post and, with the model loaded, every post has its chunks. A marker file records the pass, so no later start copies again. Run `kb state --help` for the state check subcommands.

## Roll back

1. Revert the code on the node.
2. Run `kb export-back` before the Python journal starts. It copies every post written since the cutover into the frozen tables.

**WARNING** Restore `.pre-rust/` only when `kb state check` proves damage. A restore discards every post written since the snapshot.
