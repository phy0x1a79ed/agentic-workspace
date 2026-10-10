# kb

The workspace knowledgebase as an awm service. It supervises the kb server from `projects/kb`, feeds it this node's scope posts, and relays its recall as `kb_<verb>` tools. `scope_fetch(query=...)` ranks with it once it holds every post.

## Purpose & Contents

This file holds the contract between awm and the kb server: what the child runs under, which keys it gets, where its store lives, what makes `scope_fetch` trust it, what a foreign peer may recall, and what must stay in step with the scopes service. How the server works, and why it uses Cognee the way it does, is in `projects/kb`'s `AGENTS.md`.

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

## The completeness gate

`scope_fetch` with a query calls `kb recall` with `require_complete`. kb refuses until its last full sweep listed every post and every post is ingested. Any refusal, error or 5 s timeout sends the search to the scopes service's own index. A kb that is new, ingesting, stopped or absent therefore never narrows what a search can find. The reply's `semantic` field says which index answered.

Searches filtered by author or time stay on the local index, because kb cannot filter on them. `SCOPES_KB_RECALL=0` keeps every search local.

A new post makes kb incomplete until it is ingested, about a minute. Searches meanwhile answer locally.

## The feed

Two paths keep kb current. The `posts` subscription forwards each new post. The sweep pages every post through `scope_fetch` at start and hourly, and sooner after a forward fails. It upserts them and sends kb the full id set, so kb forgets deleted posts.

**CAUTION** `feed.KINDS` must equal the scopes service's `search_index.INDEXED_KINDS`. If kb holds fewer kinds, `scope_fetch` routed to kb silently finds less than the local index would.

Journals are per-node, so the feed and the gate use the local gateway, never a peer's kb.

## A foreign peer's recall

A foreign peer holding the `kb` grant may call `status` and `recall`, both declared `read` with category `kb` (`FEDERATION.md` § *The foreign gate*). `status` returns counts and nothing else. `recall` accepts the hybrid, vector and lexical modes only. Those modes rank rows from kb's own store and honour `sources`. The `graph` and `answer` modes run Cognee with access control off, where `datasets` does not scope, and `answer` spends LLM budget on the caller's behalf. A refused mode raises `PermissionError`, which the gateway returns as the same 404 an unknown tool gets.

Posts are journal content. A foreign peer without the `journals` grant gets papers only, whatever `sources` it names.
