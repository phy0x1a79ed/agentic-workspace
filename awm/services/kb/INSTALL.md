# kb

The paper knowledgebase as an awm service. It supervises the kb server from `projects/kb`, relays its opt-in Cognee recall as `kb_<verb>` tools, and feeds the Zotero library to the `search` service as the archive source `kb`. The tool is off the agent surface (`tier = "off"`): agents find papers through `search query`.

## Purpose & Contents

This file holds the contract between awm and the kb server: what the child runs under, which keys it gets, where its store lives, what the archive source `kb` serves, and what a foreign peer may recall. How the server works, and why it uses Cognee the way it does, is in `projects/kb`'s `AGENTS.md`.

## The child

The server is `python -m kb.server` from the kb checkout, run by the `kb` env's interpreter. It starts its own ingest worker. Cognee's dependencies never enter the awm env, whose torch pin is load-bearing.

- **Checkout.** `KB_CHECKOUT`, default `projects/kb/release`.
- **Interpreter.** `install.sh` builds the `kb` env from the checkout's `envs/kb/base.yml` and records its python in `kb-python`. Under systemd there is no `mamba` to find it at runtime.
- **Port.** `KB_PORT`, default 12531, on loopback. Only this service talks to it.
- **Process tree.** The server runs in its own session with `PR_SET_PDEATHSIG`, and the worker carries the same signal from the server. One group kill stops both, and neither outlives its parent.

**CAUTION** The child's `PYTHONPATH` is replaced, not extended. A dev sandbox puts awm's source there, and awm code must never load in the kb interpreter.

**CAUTION** compute's `PROTECTED` names `kb.server` and `kb.worker`. Both run in their own session, so the `awm-service` pattern does not cover them, and the worker holds a batch for minutes. A rename of either module must update that pattern.

## Keys

| key | source | needed for |
|---|---|---|
| OpenRouter | `~/.local/share/opencode/auth.json`, handed to the child as `LLM_API_KEY` | `recall` modes `answer` and `graph` only |
| Zotero | `ZOTERO_API_KEY` in the workspace `.awm/env` | PDF full text |

Without a Zotero key, `kb sync` reads the zotero service's mirror at `projects/trilium/release/data/vault/zotero/library.json`. That gives metadata and abstracts, no PDFs. Ingest is chunk-only by default and spends nothing.

## The store

The store is `live/` in the kb checkout (`KB_LIVE`), beside the code that writes it. It is untracked.

**WARNING** Never `dvc add` `live/`. A pinned file is a read-only hardlink into the shared cache, and the server writes the store constantly.

`kb snapshot` pauses ingest, stops the server, copies `live/` to `data/store/` and runs `dvc add` on the copy. Committing the pin is left to the caller. The copy skips the embedding model cache, which re-downloads.

## The archive source

`service.toml` declares one `[[archive]]` table named `kb`, in index mode, exported by the hidden `kb_export`. The name is also the federation grant, so a peer's `kb` grant keeps its meaning. Posts are not in this source: the `journal` service owns them and `search` indexes them from there.

`kb_export` reads the Zotero mirror (`KB_ZOTERO_LIBRARY`) itself, never the kb server, so the source stays current while the server is down or ingesting. It pages the mirror in key order, keyed on the last key sent. `cursor` is the previous page's `next_cursor`. `limit` is the page size, default 200, at most 1000. Each document is `id` (the Zotero key), `title`, `text`, `ts`, `kind` (`paper`) and, when the item has one, `url`. `ts` is the publication year's first day, else the mirror's pull time. `text` matches what the kb server indexes. The last page sets `done` and also returns `ids`, every key in the library, so `search` can drop papers Zotero deleted. A missing mirror raises, and never reports an empty id set. A foreign caller is refused.

**CAUTION** The documents are metadata and abstracts, with no PDF text, because no node holds a `ZOTERO_API_KEY`. The key changes what the kb server's own recall reads. It does not change this export.

## A foreign peer's recall

A foreign peer holding the `kb` grant may call `status` and `recall`, both declared `read` with category `kb` (`FEDERATION.md` § *The foreign gate*). `status` returns counts and nothing else. `recall` always reads papers, and accepts the hybrid, vector and lexical modes only. Those modes rank rows from kb's own store. The `graph` and `answer` modes run Cognee with access control off, where `datasets` does not scope, and `answer` spends LLM budget on the caller's behalf. A refused mode raises `PermissionError`, which the gateway returns as the same 404 an unknown tool gets.
